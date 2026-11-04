#!/bin/bash
BASE_DEVICE=$1
MOUNT=$2
umount ${BASE_DEVICE}1 2>/dev/null
umount ${BASE_DEVICE}2 2>/dev/null
umount ${MOUNT} 2>/dev/null
umount -l ${MOUNT} 2>/dev/null
sleep 1
udisksctl power-off -b ${BASE_DEVICE} || eject ${BASE_DEVICE}
