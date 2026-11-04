#!/bin/bash
BASE_DEVICE=$1
FW_PATH=$2

echo "Unmounting device..."
umount ${BASE_DEVICE}1 2>/dev/null
umount ${BASE_DEVICE}2 2>/dev/null
umount ${BASE_DEVICE} 2>/dev/null
sleep 1

echo "Cleaning partition table..."
wipefs -a ${BASE_DEVICE} 2>/dev/null
dd if=/dev/zero of=${BASE_DEVICE} bs=512 count=1 2>/dev/null

echo "Creating partitions (39.2MB + Rest)..."
echo -e "label: dos\n,80262,0\n,,b" | sfdisk ${BASE_DEVICE}

echo "Updating kernel..."
partprobe ${BASE_DEVICE}
sleep 3

echo "Formatting data (FAT32)..."
mkfs.vfat -F 32 -n IPOD ${BASE_DEVICE}2

echo "Writing firmware..."
dd if=${FW_PATH} of=${BASE_DEVICE}1 bs=1M conv=fsync
sync

echo "Ejecting iPod..."
eject ${BASE_DEVICE}

echo "Restore complete!"
