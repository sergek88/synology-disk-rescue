#!/usr/bin/env python3
"""
Минимальный NBD-сервер (newstyle protocol) для чтения раздела Synology с offset.
READ-ONLY: исходное устройство открывается O_RDONLY, любые записи отклоняются.

Использование:
  sudo python3 nbd_server.py <device> <offset> <size|0=auto> <port> [bind]
Пример (btrfs внутри volume_1 на DS723+):
  sudo python3 nbd_server.py /dev/disk4s5 14221312 0 10809
"""
import socket, struct, sys, os, threading

# --- NBD protocol constants ---
NBDMAGIC            = 0x4e42444d41474943   # "NBDMAGIC"
IHAVEOPT            = 0x49484156454f5054   # "IHAVEOPT"
NBD_REP_MAGIC       = 0x0003e889045565a9
REQUEST_MAGIC       = 0x25609513
SIMPLE_REPLY_MAGIC  = 0x67446698

# handshake flags (server -> client)
NBD_FLAG_FIXED_NEWSTYLE = 1 << 0
NBD_FLAG_NO_ZEROES      = 1 << 1
# transmission flags (server -> client)
NBD_FLAG_HAS_FLAGS  = 1 << 0
NBD_FLAG_READ_ONLY  = 1 << 1
# option types (client -> server)
NBD_OPT_EXPORT_NAME = 1
NBD_OPT_ABORT       = 2
NBD_OPT_INFO        = 6
NBD_OPT_GO          = 7
# option reply types
NBD_REP_ACK         = 1
NBD_REP_INFO        = 3
NBD_REP_ERR_UNSUP   = (1 << 31) + 1
NBD_INFO_EXPORT     = 0
# command types
NBD_CMD_READ        = 0
NBD_CMD_WRITE       = 1
NBD_CMD_DISC        = 2
NBD_CMD_FLUSH       = 3
NBD_CMD_TRIM        = 4


def recvall(sock, n):
    buf = b''
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("peer closed")
        buf += chunk
    return buf


class Export:
    def __init__(self, path, offset, size):
        self.fd = os.open(path, os.O_RDONLY)
        self.offset = offset
        if size <= 0:                       # auto: размер устройства минус offset
            dev = os.lseek(self.fd, 0, os.SEEK_END)
            size = dev - offset
            os.lseek(self.fd, 0, os.SEEK_SET)
        self.size = size

    def read(self, off, length):
        return os.pread(self.fd, length, self.offset + off)


def transmission(conn, export):
    while True:
        try:
            hdr = recvall(conn, 28)
        except EOFError:
            return
        magic, flags, cmd, handle, off, length = struct.unpack(">IHHQQI", hdr)
        if magic != REQUEST_MAGIC:
            return
        if cmd == NBD_CMD_READ:
            try:
                data = export.read(off, length)
                if len(data) < length:                  # дочитать «хвост» нулями у края
                    data += b'\x00' * (length - len(data))
                conn.sendall(struct.pack(">IIQ", SIMPLE_REPLY_MAGIC, 0, handle) + data)
            except Exception:
                conn.sendall(struct.pack(">IIQ", SIMPLE_REPLY_MAGIC, 5, handle))   # EIO
        elif cmd == NBD_CMD_DISC:
            return
        elif cmd == NBD_CMD_FLUSH:
            conn.sendall(struct.pack(">IIQ", SIMPLE_REPLY_MAGIC, 0, handle))
        elif cmd == NBD_CMD_WRITE:
            recvall(conn, length)                                                  # read-only
            conn.sendall(struct.pack(">IIQ", SIMPLE_REPLY_MAGIC, 1, handle))        # EPERM
        else:
            conn.sendall(struct.pack(">IIQ", SIMPLE_REPLY_MAGIC, 1, handle))        # EPERM/unsup


def handle_client(conn, export):
    try:
        conn.sendall(struct.pack(">QQH", NBDMAGIC, IHAVEOPT,
                                 NBD_FLAG_FIXED_NEWSTYLE | NBD_FLAG_NO_ZEROES))
        client_flags = struct.unpack(">I", recvall(conn, 4))[0]
        while True:
            _magic, opt, length = struct.unpack(">QII", recvall(conn, 16))
            data = recvall(conn, length) if length else b''
            tflags = NBD_FLAG_HAS_FLAGS | NBD_FLAG_READ_ONLY
            if opt == NBD_OPT_EXPORT_NAME:
                payload = struct.pack(">QH", export.size, tflags)
                if not (client_flags & NBD_FLAG_NO_ZEROES):
                    payload += b'\x00' * 124
                conn.sendall(payload)
                transmission(conn, export)
                return
            elif opt in (NBD_OPT_INFO, NBD_OPT_GO):
                info = struct.pack(">HQH", NBD_INFO_EXPORT, export.size, tflags)
                conn.sendall(struct.pack(">QIII", NBD_REP_MAGIC, opt, NBD_REP_INFO, len(info)) + info)
                conn.sendall(struct.pack(">QIII", NBD_REP_MAGIC, opt, NBD_REP_ACK, 0))
                if opt == NBD_OPT_GO:
                    transmission(conn, export)
                    return
            elif opt == NBD_OPT_ABORT:
                conn.sendall(struct.pack(">QIII", NBD_REP_MAGIC, opt, NBD_REP_ACK, 0))
                return
            else:
                conn.sendall(struct.pack(">QIII", NBD_REP_MAGIC, opt, NBD_REP_ERR_UNSUP, 0))
    except (EOFError, BrokenPipeError, ConnectionResetError, OSError):
        pass
    finally:
        conn.close()


def main():
    if len(sys.argv) < 5:
        print(__doc__)
        sys.exit(1)
    path, offset, size, port = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
    bind = sys.argv[5] if len(sys.argv) > 5 else "0.0.0.0"
    export = Export(path, offset, size)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((bind, port))
    srv.listen(8)
    print(f"NBD READ-ONLY: {path} offset={offset} size={export.size} bytes "
          f"({export.size/1024**4:.2f} TiB) on {bind}:{port}", flush=True)
    while True:
        conn, addr = srv.accept()
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        threading.Thread(target=handle_client, args=(conn, export), daemon=True).start()


if __name__ == "__main__":
    main()
