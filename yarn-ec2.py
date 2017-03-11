#!/usr/bin/env python
# -*- coding: utf-8 -*-

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

from __future__ import division, print_function, with_statement

import codecs
import hashlib
import itertools
import logging
import os
import os.path
import pipes
import random
import shutil
import string
from stat import S_IRUSR
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import time
import warnings
from datetime import datetime
from optparse import OptionParser
from sys import stderr

if sys.version < "3":
    from urllib2 import urlopen, Request, HTTPError
else:
    from urllib.request import urlopen, Request
    from urllib.error import HTTPError

    raw_input = input
    xrange = range

YARN_EC2_VERSION = "master"
YARN_EC2_DIR = os.path.dirname(os.path.realpath(__file__))

VALID_YARN_VERSIONS = set([
    "master"
])

DEFAULT_YARN_VERSION = YARN_EC2_VERSION
DEFAULT_YARN_GITHUB_REPO = "https://github.com/zhengqmark/yarn"

# Default location to get the yarn-ec2 scripts (and ami-list) from
DEFAULT_YARN_EC2_GITHUB_REPO = "https://github.com/zhengqmark/yarn-ec2"
DEFAULT_YARN_EC2_BRANCH = "master"


def setup_external_libs(libs):
    """
    Download external libraries from PyPI to YARN_EC2_DIR/lib and prepend them to our PATH.
    """
    PYPI_URL_PREFIX = "https://pypi.python.org/packages"
    YARN_EC2_LIB_DIR = os.path.join(YARN_EC2_DIR, "lib")

    if not os.path.exists(YARN_EC2_LIB_DIR):
        print("Downloading external libraries that yarn-ec2 needs from PyPI to {path}...".format(
            path=YARN_EC2_LIB_DIR
        ))
        print("This should be a one-time operation.")
        os.mkdir(YARN_EC2_LIB_DIR)

    for lib in libs:
        versioned_lib_name = "{n}-{v}".format(n=lib["name"], v=lib["version"])
        lib_dir = os.path.join(YARN_EC2_LIB_DIR, versioned_lib_name)

        if not os.path.isdir(lib_dir):
            tgz_file_path = os.path.join(YARN_EC2_LIB_DIR, versioned_lib_name + ".tar.gz")
            print(" - Downloading {lib}-{ver}...".format(lib=lib["name"], ver=lib["version"]))
            download_stream = urlopen(
                "{prefix}/{h0}/{h1}/{h2}/{lib_name}-{lib_version}.tar.gz".format(
                    prefix=PYPI_URL_PREFIX,
                    h0=lib["hash"][:2],
                    h1=lib["hash"][2:4],
                    h2=lib["hash"][4:],
                    lib_name=lib["name"],
                    lib_version=lib["version"]
                )
            )
            with open(tgz_file_path, "wb") as tgz_file:
                tgz_file.write(download_stream.read())
            with open(tgz_file_path, "rb") as tar:
                if hashlib.md5(tar.read()).hexdigest() != lib["md5"]:
                    print("ERROR: Got wrong md5sum for {lib}.".format(lib=lib["name"]), file=stderr)
                    sys.exit(1)
            tar = tarfile.open(tgz_file_path)
            tar.extractall(path=YARN_EC2_LIB_DIR)
            tar.close()
            os.remove(tgz_file_path)
            print(" - Finished downloading {lib}.".format(lib=lib["name"]))
        sys.path.insert(1, lib_dir)


# Only PyPI libraries are supported.
external_libs = [
    {
        "name": "boto",
        "version": "2.46.1",
        "hash": "b1f9cf8fa9a4a48e651294fc88446edee96f8b965f1d3ca044befc5dd7c9449b",
        "md5": "0f952cefb7631d7847da07febb2b15cd"
    }
]

setup_external_libs(external_libs)

import boto
from boto.ec2.blockdevicemapping import BlockDeviceMapping, BlockDeviceType, EBSBlockDeviceType
from boto import ec2


class UsageError(Exception):
    pass


# Configure and parse our command-line arguments
def parse_args():
    parser = OptionParser(
        prog="yarn-ec2",
        version="%prog {v}".format(v=YARN_EC2_VERSION),
        usage="%prog [options] <action> <cluster_name>\n\n"
              + "<action> can be: launch, destroy, login, stop, start")

    parser.add_option(
        "-s", "--slaves", type="int", default=1,
        help="Number of slaves to launch (default: %default)")
    parser.add_option(
        "-k", "--key-pair",
        help="Key pair to use on instances")
    parser.add_option(
        "-i", "--identity-file",
        help="SSH private key file to use for logging into instances")
    parser.add_option(
        "-p", "--profile", default=None,
        help="If you have multiple profiles (AWS or boto config), you can configure " +
             "additional, named profiles by using this option (default: %default)")
    parser.add_option(
        "-t", "--instance-type", default="c3.large",
        help="Type of instance to launch (default: %default). " +
             "WARNING: must be 64-bit; small instances won't work")
    parser.add_option(
        "-m", "--master-instance-type", default="",
        help="Master instance type (leave empty for same as instance-type)")
    parser.add_option(
        "-r", "--region", default="us-east-1",
        help="EC2 region used to launch instances in, or to find them in (default: %default)")
    parser.add_option(
        "-z", "--zone", default="",
        help="Availability zone to launch instances in, or 'all' to spread " +
             "slaves across multiple (an additional $0.01/Gb for bandwidth" +
             "between zones applies) (default: a single zone chosen at random)")
    parser.add_option(
        "-a", "--ami",
        help="Amazon Machine Image ID to use")
    parser.add_option(
        "-v", "--yarn-version", default=DEFAULT_YARN_VERSION,
        help="Version of YARN to use: 'X.Y.Z' or a specific git hash (default: %default)")
    parser.add_option(
        "--yarn-git-repo",
        default=DEFAULT_YARN_GITHUB_REPO,
        help="Github repo from which to checkout supplied commit hash (default: %default)")
    parser.add_option(
        "--yarn-ec2-git-repo",
        default=DEFAULT_YARN_EC2_GITHUB_REPO,
        help="Github repo from which to checkout yarn-ec2 (default: %default)")
    parser.add_option(
        "--yarn-ec2-git-branch",
        default=DEFAULT_YARN_EC2_BRANCH,
        help="Github repo branch of yarn-ec2 to use (default: %default)")
    parser.add_option(
        "--deploy-root-dir",
        default=None,
        help="A directory to copy into / on the first master. " +
             "Must be absolute. Note that a trailing slash is handled as per rsync: " +
             "If you omit it, the last directory of the --deploy-root-dir path will be created " +
             "in / before copying its contents. If you append the trailing slash, " +
             "the directory is not created and its contents are copied directly into /. " +
             "(default: %default).")
    parser.add_option(
        "--hadoop-major-version", default="1",
        help="Major version of Hadoop. Valid options are 1 (Hadoop 1.0.4), 2 (CDH 4.2.0), yarn " +
             "(Hadoop 2.4.0) (default: %default)")
    parser.add_option(
        "-D", metavar="[ADDRESS:]PORT", dest="proxy_port",
        help="Use SSH dynamic port forwarding to create a SOCKS proxy at " +
             "the given local address (for use with login)")
    parser.add_option(
        "--ebs-vol-size", metavar="SIZE", type="int", default=0,
        help="Size (in GB) of each EBS volume.")
    parser.add_option(
        "--ebs-vol-type", default="standard",
        help="EBS volume type (e.g. 'gp2', 'standard').")
    parser.add_option(
        "--ebs-vol-num", type="int", default=1,
        help="Number of EBS volumes to attach to each node as /vol[x]. " +
             "The volumes will be deleted when the instances terminate. " +
             "Only possible on EBS-backed AMIs. " +
             "EBS volumes are only attached if --ebs-vol-size > 0. " +
             "Only support up to 8 EBS volumes.")
    parser.add_option(
        "--placement-group", type="string", default=None,
        help="Which placement group to try and launch " +
             "instances into. Assumes placement group is already " +
             "created.")
    parser.add_option(
        "--spot-price", metavar="PRICE", type="float",
        help="If specified, launch slaves as spot instances with the given " +
             "maximum price (in dollars)")
    parser.add_option(
        "-u", "--user", default="root",
        help="The SSH user you want to connect as (default: %default)")
    parser.add_option(
        "--delete-groups", action="store_true", default=False,
        help="When destroying a cluster, delete the security groups that were created")
    parser.add_option(
        "--use-existing-master", action="store_true", default=False,
        help="Launch fresh slaves, but use an existing stopped master if possible")
    parser.add_option(
        "--user-data", type="string", default="",
        help="Path to a user-data file (most AMIs interpret this as an initialization script)")
    parser.add_option(
        "--authorized-address", type="string", default="0.0.0.0/0",
        help="Address to authorize on created security groups (default: %default)")
    parser.add_option(
        "--additional-security-group", type="string", default="",
        help="Additional security group to place the machines in")
    parser.add_option(
        "--additional-tags", type="string", default="",
        help="Additional tags to set on the machines; tags are comma-separated, while name and " +
             "value are colon separated; ex: \"Course:advcc,Project:yarn\"")
    parser.add_option(
        "--subnet-id", default=None,
        help="VPC subnet to launch instances in")
    parser.add_option(
        "--vpc-id", default=None,
        help="VPC to launch instances in")
    parser.add_option(
        "--private-ips", action="store_true", default=False,
        help="Use private IPs for instances rather than public if VPC/subnet " +
             "requires that.")
    parser.add_option(
        "--instance-initiated-shutdown-behavior", default="stop",
        choices=["stop", "terminate"],
        help="Whether instances should terminate when shut down or just stop")
    parser.add_option(
        "--instance-profile-name", default=None,
        help="IAM profile name to launch instances under")

    (opts, args) = parser.parse_args()
    if len(args) != 2:
        parser.print_help()
        sys.exit(1)
    (action, cluster_name) = args

    # Boto config check
    # http://boto.cloudhackers.com/en/latest/boto_config_tut.html
    home_dir = os.getenv('HOME')
    if home_dir is None or not os.path.isfile(home_dir + '/.boto'):
        if not os.path.isfile('/etc/boto.cfg'):
            # If there is no boto config, check aws credentials
            if not os.path.isfile(home_dir + '/.aws/credentials'):
                if os.getenv('AWS_ACCESS_KEY_ID') is None:
                    print("ERROR: The environment variable AWS_ACCESS_KEY_ID must be set",
                          file=stderr)
                    sys.exit(1)
                if os.getenv('AWS_SECRET_ACCESS_KEY') is None:
                    print("ERROR: The environment variable AWS_SECRET_ACCESS_KEY must be set",
                          file=stderr)
                    sys.exit(1)
    return (opts, action, cluster_name)


def get_validate_yarn_version(version, repo):
    if "." in version:
        version = version.replace("v", "")
        if version not in VALID_YARN_VERSIONS:
            print("Don't know about YARN version: {v}".format(v=version), file=stderr)
            sys.exit(1)
        return version
    else:
        github_commit_url = "{repo}/commit/{commit_hash}".format(repo=repo, commit_hash=version)
        request = Request(github_commit_url)
        request.get_method = lambda: 'HEAD'
        try:
            response = urlopen(request)
        except HTTPError as e:
            print("Couldn't validate YARN commit: {url}".format(url=github_commit_url),
                  file=stderr)
            print("Received HTTP response code of {code}.".format(code=e.code), file=stderr)
            sys.exit(1)
        return version


# Source: http://aws.amazon.com/amazon-linux-ami/instance-type-matrix/
# Last Updated: 2017-03-11
# For easy maintainability, please keep this manually-inputted dictionary sorted by key.
EC2_INSTANCE_TYPES = {
    "c3.large": "hvm",
    "c3.xlarge": "hvm",
    "c3.2xlarge": "hvm",
    "c3.4xlarge": "hvm",
    "c3.8xlarge": "hvm",
    "c4.large": "hvm",
    "c4.xlarge": "hvm",
    "c4.2xlarge": "hvm",
    "c4.4xlarge": "hvm",
    "c4.8xlarge": "hvm",
    "m3.medium": "hvm",
    "m3.large": "hvm",
    "m3.xlarge": "hvm",
    "m3.2xlarge": "hvm",
    "m4.large": "hvm",
    "m4.xlarge": "hvm",
    "m4.2xlarge": "hvm",
    "m4.4xlarge": "hvm",
    "m4.10xlarge": "hvm",
    "m4.16xlarge": "hvm",
    "r3.large": "hvm",
    "r3.xlarge": "hvm",
    "r3.2xlarge": "hvm",
    "r3.4xlarge": "hvm",
    "r3.8xlarge": "hvm",
    "r4.large": "hvm",
    "r4.xlarge": "hvm",
    "r4.2xlarge": "hvm",
    "r4.4xlarge": "hvm",
    "r4.8xlarge": "hvm",
    "r4.16xlarge": "hvm",
    "t2.nano": "hvm",
    "t2.micro": "hvm",
    "t2.small": "hvm",
    "t2.medium": "hvm",
    "t2.large": "hvm",
    "t2.xlarge": "hvm",
    "t2.2xlarge": "hvm",
}


def real_main():
    (opts, action, cluster_name) = parse_args()

    # Input parameter validation
    get_validate_yarn_version(opts.yarn_version, opts.yarn_git_repo)

    # Ensure identity file
    if opts.identity_file is not None:
        if not os.path.exists(opts.identity_file):
            print("ERROR: The identity file '{f}' doesn't exist.".format(f=opts.identity_file),
                  file=stderr)
            sys.exit(1)

        file_mode = os.stat(opts.identity_file).st_mode
        if not (file_mode & S_IRUSR) or not oct(file_mode)[-2:] == '00':
            print("ERROR: The identity file must be accessible only by you.", file=stderr)
            print('You can fix this with: chmod 400 "{f}"'.format(f=opts.identity_file),
                  file=stderr)
            sys.exit(1)

    if opts.instance_type not in EC2_INSTANCE_TYPES:
        print("Warning: Unrecognized EC2 instance type for instance-type: {t}".format(
            t=opts.instance_type), file=stderr)

        if opts.master_instance_type != "":
            if opts.master_instance_type not in EC2_INSTANCE_TYPES:
                print("Warning: Unrecognized EC2 instance type for master-instance-type: {t}".format(
                    t=opts.master_instance_type), file=stderr)
        # Since we try instance types even if we can't resolve them, we check if they resolve first
        # and, if they do, see if they resolve to the same VM type.
        if opts.instance_type in EC2_INSTANCE_TYPES and \
                        opts.master_instance_type in EC2_INSTANCE_TYPES:
            if EC2_INSTANCE_TYPES[opts.instance_type] != \
                    EC2_INSTANCE_TYPES[opts.master_instance_type]:
                print("Error: yarn-ec2 currently does not support having a master and slaves "
                      "with different AMI virtualization types.", file=stderr)
                print("master instance virtualization type: {t}".format(
                    t=EC2_INSTANCE_TYPES[opts.master_instance_type]), file=stderr)
                print("slave instance virtualization type: {t}".format(
                    t=EC2_INSTANCE_TYPES[opts.instance_type]), file=stderr)
                sys.exit(1)


def main():
    try:
        real_main()
    except UsageError as e:
        print("\nError:\n", e, file=stderr)
        sys.exit(1)


if __name__ == "__main__":
    logging.basicConfig()
    main()
