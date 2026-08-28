#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Определяет, где на диске Synology начинается файловая система.

Зачем это нужно. Диск из Synology нельзя просто подключить и открыть: данные
лежат под тремя слоями. Сначала MD RAID (даже если диск был один), под ним
LVM, и уже внутри — btrfs. Чтобы добраться до файлов с чужой машины, нужно
точное смещение в байтах от начала раздела. Считать его вручную по
документации — долго и легко ошибиться на один сектор, а ошибка означает
«файловая система не найдена» без всякой подсказки, что не так.

Скрипт читает служебные заголовки прямо с диска и складывает смещения сам:

    MD RAID   — суперблок версии 1.2 лежит на 4096 байте от начала раздела,
                в нём поле data_offset: сколько секторов занимает служебная
                область перед данными.
    LVM       — заголовок физического тома и текстовые метаданные, откуда
                берутся pe_start (начало первого экстента) и расположение
                логических томов.
    btrfs     — проверяем, что по вычисленному адресу действительно она:
                на смещении 65536 внутри тома лежит подпись _BHRfS_M.

Работает только на чтение. Ничего никуда не пишет.

Запуск:
    sudo python3 syno_offset.py /dev/disk4s5      (macOS)
    sudo python3 syno_offset.py /dev/sdb3         (Linux)
"""
import os
import re
import struct
import sys

СЕКТОР = 512
MD_МАГИЯ = b"\xfc\x4e\x2b\xa9"          # суперблок MD RAID 1.x
MD_СМЕЩЕНИЕ = 4096                       # где он лежит при версии 1.2
BTRFS_МАГИЯ = b"_BHRfS_M"
BTRFS_СУПЕР = 65536                      # смещение суперблока внутри тома
LVM_МЕТКА = b"LABELONE"


def прочитать(fd, смещение, сколько):
    return os.pread(fd, сколько, смещение)


def разобрать_md(fd):
    """Возвращает data_offset в байтах или None, если MD RAID тут нет."""
    блок = прочитать(fd, MD_СМЕЩЕНИЕ, 512)
    if блок[:4] != MD_МАГИЯ:
        return None
    # Раскладка суперблока 1.x: data_offset — 8 байт по смещению 128.
    data_offset = struct.unpack_from("<Q", блок, 128)[0]
    версия = struct.unpack_from("<I", блок, 4)[0]
    уровень = struct.unpack_from("<i", блок, 72)[0]
    return {"data_offset_секторов": data_offset,
            "data_offset_байт": data_offset * СЕКТОР,
            "версия": версия, "уровень_raid": уровень}


def разобрать_lvm(fd, начало_данных):
    """Ищет метку LVM и вытаскивает pe_start и список логических томов.

    Метаданные LVM хранятся текстом — это редкая удача: их можно прочитать
    глазами и не гадать о формате. Ищем в первом мегабайте после начала.
    """
    кусок = прочитать(fd, начало_данных, 1024 * 1024)
    if LVM_МЕТКА not in кусок[:8192]:
        return None

    текст = кусок.decode("latin-1", errors="ignore")

    pe_start = None
    м = re.search(r"pe_start\s*=\s*(\d+)", текст)
    if м:
        pe_start = int(м.group(1))

    extent_size = None
    м = re.search(r"extent_size\s*=\s*(\d+)", текст)
    if м:
        extent_size = int(м.group(1))

    имя_группы = None
    м = re.search(r"^(\w+)\s*\{", текст, re.M)
    if м:
        имя_группы = м.group(1)

    # Логические тома: имя, размер в экстентах и с какого экстента начинается.
    тома = []
    for блок_lv in re.finditer(
            r"(\w+)\s*\{[^{}]*?segment_count\s*=\s*\d+(.*?)\n\t*\}", текст, re.S):
        имя = блок_lv.group(1)
        тело = блок_lv.group(2)
        нач = re.search(r"start_extent\s*=\s*(\d+)", тело)
        кол = re.search(r"extent_count\s*=\s*(\d+)", тело)
        ст = re.search(r"stripes\s*=\s*\[\s*\"([^\"]+)\"\s*,\s*(\d+)", тело)
        if нач and кол:
            тома.append({
                "имя": имя,
                "экстентов": int(кол.group(1)),
                "начальный_экстент_в_pv": int(ст.group(2)) if ст else None,
            })

    return {"группа": имя_группы, "pe_start_секторов": pe_start,
            "extent_size_секторов": extent_size, "тома": тома}


def это_btrfs(fd, смещение):
    подпись = прочитать(fd, смещение + BTRFS_СУПЕР + 64, 8)
    return подпись == BTRFS_МАГИЯ


def размер_устройства(fd):
    try:
        return os.lseek(fd, 0, os.SEEK_END)
    except OSError:
        return 0


def главное():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    путь = sys.argv[1]
    try:
        fd = os.open(путь, os.O_RDONLY)
    except PermissionError:
        print("Нет доступа к %s — запустите через sudo." % путь)
        return 1
    except OSError as e:
        print("Не открылось %s: %s" % (путь, e))
        return 1

    print("Устройство: %s" % путь)
    всего = размер_устройства(fd)
    if всего:
        print("Размер: %.2f ТБ" % (всего / 1000 ** 4))

    md = разобрать_md(fd)
    if not md:
        print("\nMD RAID не найден.")
        print("Возможные причины: это не раздел с данными (у Synology их три,"
              " нужен самый большой), либо том собран иначе.")
        os.close(fd)
        return 1
    print("\nMD RAID: версия 1.%d, уровень %d" % (md["версия"], md["уровень_raid"]))
    print("  служебная область: %d секторов = %d байт"
          % (md["data_offset_секторов"], md["data_offset_байт"]))

    lvm = разобрать_lvm(fd, md["data_offset_байт"])
    if not lvm:
        # Бывает и без LVM — тогда файловая система сразу за MD.
        if это_btrfs(fd, md["data_offset_байт"]):
            print("\nLVM нет, btrfs лежит сразу за MD RAID.")
            print("\nСМЕЩЕНИЕ ДЛЯ NBD: %d" % md["data_offset_байт"])
            os.close(fd)
            return 0
        print("\nLVM не найден, и btrfs по этому адресу тоже нет.")
        os.close(fd)
        return 1

    print("\nLVM: группа томов «%s»" % (lvm["группа"] or "?"))
    print("  первый экстент начинается на секторе %s" % lvm["pe_start_секторов"])
    print("  размер экстента: %s секторов (%.0f МБ)"
          % (lvm["extent_size_секторов"],
             (lvm["extent_size_секторов"] or 0) * СЕКТОР / 1024 ** 2))
    for т in lvm["тома"]:
        print("  том «%s»: %d экстентов, начинается с экстента %s"
              % (т["имя"], т["экстентов"], т["начальный_экстент_в_pv"]))

    # Складываем слои и проверяем каждый кандидат подписью btrfs.
    print("\nПРОВЕРЯЮ, ГДЕ ЛЕЖИТ ФАЙЛОВАЯ СИСТЕМА")
    нашли = False
    for т in lvm["тома"]:
        нач = т["начальный_экстент_в_pv"]
        if нач is None or not lvm["pe_start_секторов"] or not lvm["extent_size_секторов"]:
            continue
        смещение = (md["data_offset_байт"]
                    + lvm["pe_start_секторов"] * СЕКТОР
                    + нач * lvm["extent_size_секторов"] * СЕКТОР)
        размер = т["экстентов"] * lvm["extent_size_секторов"] * СЕКТОР
        есть = это_btrfs(fd, смещение)
        print("  «%s»: смещение %d — %s"
              % (т["имя"], смещение, "btrfs НАЙДЕНА" if есть else "не btrfs"))
        if есть:
            нашли = True
            print("\n" + "=" * 62)
            print("СМЕЩЕНИЕ: %d байт" % смещение)
            print("РАЗМЕР:   %d байт (%.2f ТБ)" % (размер, размер / 1000 ** 4))
            print("=" * 62)
            print("\nЗапускать сервер так:")
            print("  sudo python3 nbd_server.py %s %d %d 10809"
                  % (путь, смещение, размер))

    if not нашли:
        print("\nНи один том не опознан как btrfs.")
        print("Если том зашифрован или это ext4, подпись будет другой —"
              " смотрите README, раздел «Когда не сработало».")
    os.close(fd)
    return 0 if нашли else 1


if __name__ == "__main__":
    sys.exit(главное())
