#!/bin/bash -xu

#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

exec 1>&2

# Install system updates
sudo apt-get update && sudo apt-get -y upgrade

sudo apt-get install -y curl vim realpath lxc lvm2

pushd $HOME > /dev/null

PRIMARY_IP=`curl http://169.254.169.254/latest/meta-data/local-ipv4`
MAC=`curl http://169.254.169.254/latest/meta-data/mac`
CIDR=`curl http://169.254.169.254/latest/meta-data/network/interfaces/macs/$MAC/subnet-ipv4-cidr-block`
PRIVATE_IPS=`curl http://169.254.169.254/latest/meta-data/network/interfaces/macs/$MAC/local-ipv4s`
echo "$PRIVATE_IPS" > my_ips

MASK=`echo $CIDR | cut -d/ -f2`
DEV=`ls -1 /sys/class/net/ | fgrep -v lxc | fgrep -v lo | head -1`

sudo ip addr show dev $DEV
sudo ip addr flush secondary dev $DEV || exit 1
for ipv4 in `cat my_ips` ; do
    if [ x"$ipv4" != x"$PRIMARY_IP" ] ; then
        sudo ip addr add "$ipv4/$MASK" brd + dev $DEV || exit 1
    fi
done
sudo ip addr show dev $DEV

sudo df -h

DISKS=`lsblk -ln | fgrep -v part | fgrep -v lvm | fgrep -v da | cut -d' ' -f1`
echo "$DISKS" | awk '{print "/dev/" $0}' > my_disks
NUM_DISKS=`cat my_disks | wc -l`

sudo lsblk
sudo umount -f /mnt &>/dev/null
if [ -f /dev/yarn-vg/yarn-lv ] ; then
    sudo umount -f /dev/yarn-vg/yarn-lv &>/dev/null
    sudo lvremove -f /dev/yarn-vg/yarn-lv
fi
if [ -f /dev/yarn-vg ] ; then
    sudo vgremove -f /dev/yarn-vg
fi
if [ $NUM_DISKS -gt 0 ] ; then
    for dev in `cat my_disks` ; do sudo pvcreate -ff -y $dev || exit 1 ; done
    sudo vgcreate -y yarn-vg `cat my_disks | paste -sd ' ' -` || exit 1
    sudo lvcreate -l 100%FREE -n yarn-lv yarn-vg || exit 1
    sudo mkfs.xfs -f /dev/yarn-vg/yarn-lv || exit 1
    sudo mount /dev/yarn-vg/yarn-lv /mnt || exit 1
fi
sudo lsblk

sudo df -h

popd > /dev/null

exit 0
