#!/bin/bash

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

set -euxo pipefail

exec 1>&2

# Install system updates
sudo apt-get update && sudo apt-get -y upgrade

sudo apt-get install -y pdsh

# Load the cluster variables set by the deploy script
if [ -f $HOME/etc/yarn-ec2.rc ] ; then
    source $HOME/etc/yarn-ec2.rc
fi

mkdir -p $HOME/var/yarn-ec2 && rm -rf $HOME/var/yarn-ec2/*

pushd $HOME/var/yarn-ec2 > /dev/null

mkdir -p $HOME/tmp

SSH_OPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=5"
export PDSH_SSH_ARGS_APPEND="$SSH_OPTS"
PDSH="pdsh -S -R ssh -b"

echo "Setting up YARN on `hostname`..." > /dev/null
# Set up the masters, slaves, etc files based on cluster env variables
echo "$MASTERS" | sed '/^$/d' > masters
echo "$SLAVES" | sed '/^$/d' > slaves
cat masters slaves > all-nodes
NRACKS=`cat all-nodes | wc -l`
rm -f hosts

function setup_rack() {
### @param rack_id, rack_ips ###
    RACKDIR=`echo rack-"$1"`
    mkdir -p $RACKDIR
    VMINFO=`cat $HOME/etc/yarn-topo.txt | fgrep $RACKDIR`
    echo $VMINFO | cut -d' ' -f4 > $RACKDIR/vmncpus
    echo $VMINFO | cut -d' ' -f3 > $RACKDIR/vmmem
    H=0
    CAP=`echo $VMINFO | cut -d' ' -f2`
    echo "$2" | head -n $CAP > $RACKDIR/vmips
    echo `cat all-nodes | head -n $(( $1 + 1 )) | tail -n 1` r"$1" >> hosts
    for ip in `cat $RACKDIR/vmips` ; do
        echo $ip r"$1"h"$H" >> hosts
        H=$(( H + 1 ))
    done
}

[ $NRACKS -gt 0 ] && setup_rack 0 "$RACK0"
[ $NRACKS -gt 1 ] && setup_rack 1 "$RACK1"
[ $NRACKS -gt 2 ] && setup_rack 2 "$RACK2"
[ $NRACKS -gt 3 ] && setup_rack 3 "$RACK3"
[ $NRACKS -gt 4 ] && setup_rack 4 "$RACK4"

echo "Setting executable permissions on scripts..." > /dev/null
find $HOME/share/yarn-ec2 -regex "^.+\.sh$" | xargs chmod a+x
echo "RSYNC'ing packages to other cluster nodes..." > /dev/null
for node in `cat slaves` ; do
    echo $node > /dev/null
    rsync -e "ssh $SSH_OPTS" -az $HOME/share/yarn-ec2 \
        $node:$HOME/share &
    sleep 0.1
    rsync -e "ssh $SSH_OPTS" -az $HOME/var/yarn-ec2 \
        $node:$HOME/var &
    sleep 0.1
done

wait

echo "Setting up cluster nodes..." > /dev/null
$PDSH -w ^all-nodes $HOME/share/yarn-ec2/setup-slave.sh \
    2>&1 | tee $HOME/tmp/setup-slaves.log
env JAVA_HOME=/usr/lib/jvm/default-java HADOOP_HOME=/usr/local/hd \
    HADOOP_CONF_DIR=/tmp/hd/conf HADOOP_LOG_DIR=/tmp/hd/logs HADOOP_PID_DIR=/tmp \
    /tmp/hd/bin/hdfs namenode -format -force
$PDSH -w ^all-nodes $HOME/share/yarn-ec2/start-slave.sh \
    2>&1 | tee $HOME/tmp/start-slaves.log

popd > /dev/null

exit 0
