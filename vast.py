#!/usr/bin/env python3

from __future__ import unicode_literals, print_function

import logging
import re
import json
import sys
import argparse
import os
import tempfile
import time
import typing
import hashlib
import uuid
from tqdm import tqdm
from datetime import date, datetime
from pathlib import Path
from random import random
from zipfile import ZipFile

from gitignore_parser import parse_gitignore
from paramiko import ChannelFile, SSHClient, Channel
from paramiko.ssh_exception import NoValidConnectionsError
from scp import SCPClient, SCPException

import paramiko
import requests
import getpass
import subprocess
from subprocess import PIPE

try:
    from urllib import quote_plus  # Python 2.X
except ImportError:
    from urllib.parse import quote_plus  # Python 3+

try:
    JSONDecodeError = json.JSONDecodeError
except AttributeError:
    JSONDecodeError = ValueError

try:
    input = raw_input
except NameError:
    pass


#server_url_default = "https://vast.ai"
server_url_default = "https://console.vast.ai"
#server_url_default  = "https://vast.ai/api/v0"
api_key_file_base = "~/.vast_api_key"
api_key_file = os.path.expanduser(api_key_file_base)
api_key_guard = object()

_app_path = '/app'


class Object(object):
    pass


class argument(object):
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs


class hidden_aliases(object):
    # just a bit of a hack
    def __init__(self, l):
        self.l = l

    def __iter__(self):
        return iter(self.l)

    def __bool__(self):
        return False

    def __nonzero__(self):
        return False

    def append(self, x):
        self.l.append(x)


class apwrap(object):
    def __init__(self, *args, **kwargs):
        kwargs["formatter_class"] = argparse.RawDescriptionHelpFormatter
        self.parser = argparse.ArgumentParser(*args, **kwargs)
        self.parser.set_defaults(func=self.fail_with_help)
        self.subparsers_ = None
        self.subparser_objs = []
        self.added_help_cmd = False
        self.post_setup = []
        self.verbs = set()
        self.objs = set()

    def fail_with_help(self, *a, **kw):
        self.parser.print_help(sys.stderr)
        raise SystemExit

    def add_argument(self, *a, **kw):
        if not kw.get("parent_only"):
            for x in self.subparser_objs:
                try:
                    x.add_argument(*a, **kw)
                except argparse.ArgumentError:
                    # duplicate - or maybe other things, hopefully not
                    pass
        return self.parser.add_argument(*a, **kw)

    def subparsers(self, *a, **kw):
        if self.subparsers_ is None:
            kw["metavar"] = "command"
            kw["help"] = "command to run. one of:"
            self.subparsers_ = self.parser.add_subparsers(*a, **kw)
        return self.subparsers_

    def get_name(self, verb, obj):
        if obj:
            self.verbs.add(verb)
            self.objs.add(obj)
            name = verb + ' ' + obj
        else:
            self.objs.add(verb)
            name = verb
        return name

    def command(self, *arguments, aliases=(), help=None, **kwargs):
        help_ = help
        if not self.added_help_cmd:
            self.added_help_cmd = True

            @self.command(argument("subcommand", default=None, nargs="?"), help="print this help message")
            def help(*a, **kw):
                self.fail_with_help()

        def inner(func):
            dashed_name = func.__name__.replace("_", "-")
            verb, _, obj = dashed_name.partition("--")
            name = self.get_name(verb, obj)
            aliases_transformed = [] if aliases else hidden_aliases([])
            for x in aliases:
                verb, _, obj = x.partition(" ")
                aliases_transformed.append(self.get_name(verb, obj))
            kwargs["formatter_class"] = argparse.RawDescriptionHelpFormatter
            sp = self.subparsers().add_parser(name, aliases=aliases_transformed, help=help_, **kwargs)
            self.subparser_objs.append(sp)
            for arg in arguments:
                sp.add_argument(*arg.args, **arg.kwargs)
            sp.set_defaults(func=func)
            return func

        if len(arguments) == 1 and type(arguments[0]) != argument:
            func = arguments[0]
            arguments = []
            return inner(func)
        return inner

    def parse_args(self, argv=None, *a, **kw):
        if argv is None:
            argv = sys.argv[1:]
        argv_ = []
        for x in argv:
            if argv_ and argv_[-1] in self.verbs:
                argv_[-1] += " " + x
            else:
                argv_.append(x)
        args = self.parser.parse_args(argv_, *a, **kw)
        for func in self.post_setup:
            func(args)
        return args


parser = apwrap(epilog="Use 'vast COMMAND --help' for more info about a command")

def translate_null_strings_to_blanks(d: typing.Dict) -> typing.Dict:
    """Map over a dict and translate any null string values into ' '.
    Leave everything else as is. This is needed because you cannot add TableCell
    objects with only a null string or the client crashes.

    :param Dict d: dict of item values.
    :rtype Dict:
    """

    # Beware: locally defined function.
    def translate_nulls(s):
        if s == "":
            return " "
        return s

    new_d = {k: translate_nulls(v) for k, v in d.items()}
    return new_d


def apiurl(args: argparse.Namespace, subpath: str, query_args: typing.Dict = None,
           add_rnd: bool = False) -> str:
    """Creates the endpoint URL for a given combination of parameters.

    :param argparse.Namespace args: Namespace with many fields relevant to the endpoint.
    :param str subpath: added to end of URL to further specify endpoint.
    :param typing.Dict query_args: specifics such as API key and search parameters that complete the URL.
    :rtype str:
    """
    if query_args is None:
        query_args = {}
    if args.api_key is not None:
        query_args["api_key"] = args.api_key
    if add_rnd:
        query_args['.r'] = uuid.uuid4().hex
    if query_args:
        # a_list      = [<expression> for <l-expression> in <expression>]
        '''
        vector result;
        for (l_expression: expression) {
            result.push_back(expression);
        }
        '''
        # an_iterator = (<expression> for <l-expression> in <expression>)
        return args.url + "/api/v0" + subpath + "?" + "&".join(
            "{x}={y}".format(x=x, y=quote_plus(y if isinstance(y, str) else json.dumps(y))) for x, y in
            query_args.items())
    else:
        return args.url + "/api/v0" + subpath


def deindent(message: str) -> str:
    """
    Deindent a quoted string. Scans message and finds the smallest number of whitespace characters in any line and
    removes that many from the start of every line.

    :param str message: Message to deindent.
    :rtype str:
    """
    message = re.sub(r" *$", "", message, flags=re.MULTILINE)
    indents = [len(x) for x in re.findall("^ *(?=[^ ])", message, re.MULTILINE) if len(x)]
    a = min(indents)
    message = re.sub(r"^ {," + str(a) + "}", "", message, flags=re.MULTILINE)
    return message.strip()


# These are the fields that are displayed when a search is run
displayable_fields = (
    # ("bw_nvlink", "Bandwidth NVLink", "{}", None, True),
    ("id", "ID", "{}", None, True),
    ("cuda_max_good", "CUDA", "{:0.1f}", None, True),
    ("num_gpus", "N", "{}x", None, False),
    ("gpu_name", "Model", "{}", None, True),
    ("pcie_bw", "PCIE", "{:0.1f}", None, True),
    ("cpu_cores_effective", "vCPUs", "{:0.1f}", None, True),
    ("cpu_ram", "RAM", "{:0.1f}", lambda x: x / 1000, False),
    ("disk_space", "Disk", "{:.0f}", None, True),
    ("dph_total", "$/hr", "{:0.4f}", None, True),
    ("dlperf", "DLP", "{:0.1f}", None, True),
    ("dlperf_per_dphtotal", "DLP/$", "{:0.2f}", None, True),
    ("driver_version", "NV Driver", "{}", None, True),
    ("inet_up", "Net_up", "{:0.1f}", None, True),
    ("inet_down", "Net_down", "{:0.1f}", None, True),
    ("reliability2", "R", "{:0.1f}", lambda x: x * 100, True),
    ("duration", "Max_Days", "{:0.1f}", lambda x: x / (24.0 * 60.0 * 60.0), True),
    ("machine_id", "mach_id", "{}", None, True),
    ("verification", "status", "{}", None, True),
    ("direct_port_count", "ports", "{}", None, True),
    ("geolocation", "country", "{}", None, True),
   #  ("direct_port_count", "Direct Port Count", "{}", None, True),
)


# Need to add bw_nvlink, machine_id, direct_port_count to output.


# These fields are displayed when you do 'show instances'
instance_fields = (
    ("id", "ID", "{}", None, True),
    ("machine_id", "Machine", "{}", None, True),
    ("actual_status", "Status", "{}", None, True),
    ("num_gpus", "Num", "{}x", None, False),
    ("gpu_name", "Model", "{}", None, True),
    ("gpu_util", "Util. %", "{:0.1f}", None, True),
    ("cpu_cores_effective", "vCPUs", "{:0.1f}", None, True),
    ("cpu_ram", "RAM", "{:0.1f}", lambda x: x / 1000, False),
    ("disk_space", "Storage", "{:.0f}", None, True),
    ("ssh_host", "SSH Addr", "{}", None, True),
    ("ssh_port", "SSH Port", "{}", None, True),
    ("dph_total", "$/hr", "{:0.4f}", None, True),
    ("image_uuid", "Image", "{}", None, True),
    # ("dlperf",              "DLPerf",   "{:0.1f}",  None, True),
    # ("dlperf_per_dphtotal", "DLP/$",    "{:0.1f}",  None, True),
    ("inet_up", "Net up", "{:0.1f}", None, True),
    ("inet_down", "Net down", "{:0.1f}", None, True),
    ("reliability2", "R", "{:0.1f}", lambda x: x * 100, True),
    ("label", "Label", "{}", None, True)

    # ("duration",            "Max Days", "{:0.1f}",  lambda x: x/(24.0*60.0*60.0), True),
)

invoice_fields = (
    ("amount", "Amount", "{}", None, True),
    ("description", "Description", "{}", None, True),
    ("quantity", "Quantity", "{}", None, True),
    ("rate", "Rate", "{}", None, True),
    ("timestamp", "Timestamp", "{:0.1f}", None, True),
    ("type", "Type", "{}", None, True)
)

user_fields = (
    # ("api_key", "api_key", "{}", None, True),
    ("balance", "Balance", "{}", None, True),
    ("balance_threshold", "Bal. Thld", "{}", None, True),
    ("balance_threshold_enabled", "Bal. Thld Enabled", "{}", None, True),
    ("billaddress_city", "City", "{}", None, True),
    ("billaddress_country", "Country", "{}", None, True),
    ("billaddress_line1", "Addr Line 1", "{}", None, True),
    ("billaddress_line2", "Addr line 2", "{}", None, True),
    ("billaddress_zip", "Zip", "{}", None, True),
    ("billed_expected", "Billed Expected", "{}", None, True),
    ("billed_verified", "Billed Vfy", "{}", None, True),
    ("billing_creditonly", "Billing Creditonly", "{}", None, True),
    ("can_pay", "Can Pay", "{}", None, True),
    ("credit", "Credit", "{:0.2f}", None, True),
    ("email", "Email", "{}", None, True),
    ("email_verified", "Email Vfy", "{}", None, True),
    ("fullname", "Full Name", "{}", None, True),
    ("got_signup_credit", "Got Signup Credit", "{}", None, True),
    ("has_billing", "Has Billing", "{}", None, True),
    ("has_payout", "Has Payout", "{}", None, True),
    ("id", "Id", "{}", None, True),
    ("last4", "Last4", "{}", None, True),
    ("paid_expected", "Paid Expected", "{}", None, True),
    ("paid_verified", "Paid Vfy", "{}", None, True),
    ("password_resettable", "Pwd Resettable", "{}", None, True),
    ("paypal_email", "Paypal Email", "{}", None, True),
    ("ssh_key", "Ssh Key", "{}", None, True),
    ("user", "User", "{}", None, True),
    ("username", "Username", "{}", None, True)
)


def version_string_sort(a, b) -> int:
    """
    Accepts two version strings and decides whether a > b, a == b, or a < b.
    This is meant as a sort function to be used for the driver versions in which only
    the == operator currently works correctly. Not quite finished...

    :param str a:
    :param str b:
    :return int:
    """
    a_parts = a.split(".")
    b_parts = b.split(".")

    return 0


def parse_query(query_str: str, res: typing.Dict = None) -> typing.Dict:
    """
    Basically takes a query string (like the ones in the examples of commands for the search__offers function) and
    processes it into a dict of URL parameters to be sent to the server.

    :param str query_str:
    :param Dict res:
    :return Dict:
    """
    if res is None: res = {}
    if type(query_str) == list:
        query_str = " ".join(query_str)
    query_str = query_str.strip()
    opts = re.findall("([a-zA-Z0-9_]+)( *[=><!]+| +(?:[lg]te?|nin|neq|eq|not ?eq|not ?in|in) )?( *)(\[[^\]]+\]|[^ ]+)?( *)", query_str)

    #print(opts)
    # res = {}
    op_names = {
        ">=": "gte",
        ">": "gt",
        "gt": "gt",
        "gte": "gte",
        "<=": "lte",
        "<": "lt",
        "lt": "lt",
        "lte": "lte",
        "!=": "neq",
        "==": "eq",
        "=": "eq",
        "eq": "eq",
        "neq": "neq",
        "noteq": "neq",
        "not eq": "neq",
        "notin": "notin",
        "not in": "notin",
        "nin": "notin",
        "in": "in",
    };

    field_alias = {
        "cuda_vers": "cuda_max_good",
        "display_active": "gpu_display_active",
        "reliability": "reliability2",
        "dlperf_usd": "dlperf_per_dphtotal",
        "dph": "dph_total",
        "flops_usd": "flops_per_dphtotal",
    };

    field_multiplier = {
        "cpu_ram": 1000,
        "gpu_ram": 1000,
        "duration": 1.0 / (24.0 * 60.0 * 60.0),
    }

    fields = {
        "bw_nvlink",
        "compute_cap",
        "cpu_cores",
        "cpu_cores_effective",
        "cpu_ram",
        "cuda_max_good",
        "direct_port_count",
        "driver_version",
        "disk_bw",
        "disk_space",
        "dlperf",
        "dlperf_per_dphtotal",
        "dph_total",
        "duration",
        "external",
        "flops_per_dphtotal",
        "gpu_display_active",
        # "gpu_ram_free_min",
        "gpu_mem_bw",
        "gpu_name",
        "gpu_ram",
        "has_avx",
        "host_id",
        "id",
        "inet_down",
        "inet_down_cost",
        "inet_up",
        "inet_up_cost",
        "machine_id",
        "min_bid",
        "mobo_name",
        "num_gpus",
        "pci_gen",
        "pcie_bw",
        "reliability2",
        "rentable",
        "rented",
        "storage_cost",
        "total_flops",
        "verification",
        "verified",

    };

    joined = "".join("".join(x) for x in opts)
    if joined != query_str:
        raise ValueError(
            "Unconsumed text. Did you forget to quote your query? " + repr(joined) + " != " + repr(query_str))
    for field, op, _, value, _ in opts:
        value = value.strip(",[]")
        v = res.setdefault(field, {})
        op = op.strip()
        op_name = op_names.get(op)

        if field in field_alias:
            field = field_alias[field];

        if not field in fields:
            print("Warning: Unrecognized field: {}, see list of recognized fields.".format(field), file=sys.stderr);
        if not op_name:
            raise ValueError("Unknown operator. Did you forget to quote your query? " + repr(op).strip("u"))
        if op_name in ["in", "notin"]:
            value = [x.strip() for x in value.split(",") if x.strip()]
        if not value:
            raise ValueError("Value cannot be blank. Did you forget to quote your query? " + repr((field, op, value)))
        if not field:
            raise ValueError("Field cannot be blank. Did you forget to quote your query? " + repr((field, op, value)))
        if value in ["?", "*", "any"]:
            if op_name != "eq":
                raise ValueError("Wildcard only makes sense with equals.")
            if field in v:
                del v[field]
            if field in res:
                del res[field]
            continue

        if field in field_multiplier:
            value = str(float(value) * field_multiplier[field]);

        v[op_name] = value.replace('_', ' ')
        res[field] = v;

    #print(res)
    return res


def display_table(rows: list, fields: typing.Tuple, max_count: typing.Optional[int] = None) -> None:
    """Basically takes a set of field names and rows containing the corresponding data and prints a nice tidy table
    of it.

    :param list rows: Each row is a dict with keys corresponding to the field names (first element) in the fields tuple.

    :param Tuple fields: 5-tuple describing a field. First element is field name, second is human readable version, third is format string, fourth is a lambda function run on the data in that field, fifth is a bool determining text justification. True = left justify, False = right justify. Here is an example showing the tuples in action.

    :param int max_count: The maximum number of table entries to show.

    :rtype None:

    Example of 5-tuple: ("cpu_ram", "RAM", "{:0.1f}", lambda x: x / 1000, False)
    """
    header = [name for _, name, _, _, _ in fields]
    out_rows = [header]
    lengths = [len(x) for x in header]
    count = 0
    for instance in rows:
        row = []
        out_rows.append(row)
        for key, name, fmt, conv, _ in fields:
            conv = conv or (lambda x: x)
            val = instance.get(key, None)
            if val is None:
                s = "-"
            else:
                val = conv(val)
                s = fmt.format(val)
            s = s.replace(' ', '_')
            idx = len(row)
            lengths[idx] = max(len(s), lengths[idx])
            row.append(s)
        count += 1
        if max_count is not None and count >= max_count:
            break
    for row in out_rows:
        out = []
        for l, s, f in zip(lengths, row, fields):
            _, _, _, _, ljust = f
            if ljust:
                s = s.ljust(l)
            else:
                s = s.rjust(l)
            out.append(s)
        print("  ".join(out))


class VRLException(Exception):
    pass

def parse_vast_url(url_str):
    """
    Breaks up a vast-style url in the form instance_id:path and does
    some basic sanity type-checking.

    :param url_str:
    :return:
    """

    instance_id = None
    path = url_str
    if (":" in url_str):
        url_parts = url_str.split(":", 2)
        if len(url_parts) == 2:
            (instance_id, path) = url_parts
        else:
            raise VRLException("Invalid VRL (Vast resource locator).")
        try:
            instance_id = int(instance_id)
        except:
            raise VRLException("Instance id must be an integer.")

    valid_unix_path_regex = re.compile('^(/)?([^/\0]+(/)?)+$')
    # Got this regex from https://stackoverflow.com/questions/537772/what-is-the-most-correct-regular-expression-for-a-unix-file-path
    if (path != "/") and (valid_unix_path_regex.match(path) is None):
        raise VRLException(f"Path component: {path} of VRL is not a valid Unix style path.")

    return (instance_id, path)



@parser.command(
    argument("src", help="instance_id:/path to source of object to copy.", type=str),
    argument("dst", help="instance_id:/path to target of copy operation.", type=str),
    argument("-i", "--identity", help="Location of ssh private key", type=str),
    usage="./vast copy src dst",
    help=" Copy directories between instances and/or local, using rsync",
    epilog=deindent("""
        Copies a directory from a source location to a target location. Each of source and destination
        directories can be either local or remote, subject to appropriate read and write
        permissions required to carry out the action. The format for both src and dst is [instance_id:]path.
        Examples:
         vast copy 11824:/data/test 12371:/temp
         vast copy 11824:/data/test data/test
         vast copy data/test 11824:/data/test

        The first example copy syncs the directory '/tmp' in instance 12371 from the directory '/data/test' in instance 11824.
        The second example copy syncs the relative directory 'data/test' on the local machine from '/data/test' in instance 11824.
        The third example copy syncs the directory '/data/test' in instance 11824 from the relative directory 'data/test' on the local machine.
    """),
)
def copy(args: argparse.Namespace):
    """
    Transfer data from one instance to another.

    @param src: Location of data object to be copied.
    @param dst: Target to copy object to.
    """

    url = apiurl(args, f"/commands/rsync/")
    (src_id, src_path) = parse_vast_url(args.src)
    (dst_id, dst_path) = parse_vast_url(args.dst)
    if (src_id is None) and (dst_id is None):
        print("invalid arguments")
        return

    print(f"copying {src_id}:{src_path} {dst_id}:{dst_path}")

    req_json = {
        "client_id": "me",
        "src_id": src_id,
        "dst_id": dst_id,
        "src_path": src_path,
        "dst_path": dst_path,
    }
    r = requests.put(url, json=req_json)
    r.raise_for_status()
    if (r.status_code == 200):
        rj = r.json()
        #print(json.dumps(rj, indent=1, sort_keys=True))
        if (rj["success"]) and ((src_id is None) or (dst_id is None)):
            homedir = subprocess.getoutput("echo $HOME")
            #print(f"homedir: {homedir}")
            remote_port = None
            identity = args.identity if (args.identity is not None) else f"{homedir}/.ssh/id_rsa"
            if (src_id is None):
                #result = subprocess.run(f"mkdir -p {src_path}", shell=True)
                remote_port = rj["dst_port"]
                remote_addr = rj["dst_addr"]
                cmd = f"sudo rsync -arz -v --progress --rsh=ssh -e 'sudo ssh -i {identity} -p {remote_port} -o StrictHostKeyChecking=no' {src_path} vastai_kaalia@{remote_addr}::{dst_id}/{dst_path}"
                print(cmd)
                result = subprocess.run(cmd, shell=True)
                #result = subprocess.run(["sudo", "rsync" "-arz", "-v", "--progress", "-rsh=ssh", "-e 'sudo ssh -i {homedir}/.ssh/id_rsa -p {remote_port} -o StrictHostKeyChecking=no'", src_path, "vastai_kaalia@{remote_addr}::{dst_id}"], shell=True)
            elif (dst_id is None):
                result = subprocess.run(f"mkdir -p {dst_path}", shell=True)
                remote_port = rj["src_port"]
                remote_addr = rj["src_addr"]
                cmd = f"sudo rsync -arz -v --progress --rsh=ssh -e 'sudo ssh -i {identity} -p {remote_port} -o StrictHostKeyChecking=no' vastai_kaalia@{remote_addr}::{src_id}/{src_path} {dst_path}"
                print(cmd)
                result = subprocess.run(cmd, shell=True)
                #result = subprocess.run(["sudo", "rsync" "-arz", "-v", "--progress", "-rsh=ssh", "-e 'sudo ssh -i {homedir}/.ssh/id_rsa -p {remote_port} -o StrictHostKeyChecking=no'", "vastai_kaalia@{remote_addr}::{src_id}", dst_path], shell=True)
        else:
            if (rj["success"]):
                print("Remote to Remote copy initiated - check instance status bar for progress updates (~30 seconds delayed).")
            else:
                print(rj["msg"]);
    else:
        print(r.text);
        print("failed with error {r.status_code}".format(**locals()));


@parser.command(
    argument("src", help="path to source directory to copy.", type=str),
    argument("dst", help="instance_id:/path to target of copy operation.", type=str),
    argument("-i", "--identity", help="Location of ssh private key", type=str),
    argument("-g", "--gitignore", help="Do not copy files matching .gitignore paths", action="store_true"),
    usage="./vast copy2 src dst",
    help="Copy a directory from local to instance using Python-based scp",
    epilog=deindent("""
        Examples:
         vast copy2 . 11824:/root
    """),
)
def copy2(args: argparse.Namespace):
    """
    Transfer data from one instance to another.

    @param src: Location of data object to be copied.
    @param dst: Target to copy object to.
    """

    (src_id, src_path) = parse_vast_url(args.src)
    (dst_id, dst_path) = parse_vast_url(args.dst)
    if dst_id is None:
        print('Error: destination path requires instance ID')
        return 1
    if src_id is not None:
        print("Error: source path must be local")
        return 1
    if not os.path.isdir(src_path):
        print('Error: source path must be a directory')
        return 1
    rel_paths = _relative_paths(src_path)
    print(f'Copying {len(rel_paths)} files from {src_path} to {dst_path} in instance {dst_id}...')
    check_gitignore = args.gitignore
    instance = _get_instance(args, dst_id)
    _scp_files(args, src_path, rel_paths, instance, dst_path, check_gitignore=check_gitignore)


@parser.command(
    argument("id", help="id of instance type (offer ID) to launch", type=int),
    argument("command", help="command to be run in the launched instance", type=str),
    argument("-i", "--identity", help="Location of ssh private key", type=str),
    argument('--timeout', help="Maximum number of seconds to wait for instance to become available.", type=float,
             default=512.0),
    argument("--price", help="per machine bid price in $/hour", type=float),
    argument("--disk", help="size of local disk partition in GB", type=float, default=10),
    argument("--image", help="docker container image to launch", type=str),
    argument("--login", help="docker login arguments for private repo authentication, surround with '' ", type=str),
    argument("--label", help="label to set on the instance", type=str),
    argument("--onstart", help="filename to use as onstart script", type=str),
    argument("--onstart-cmd", help="contents of onstart script as single argument", type=str),
    argument("--ssh",     help="Launch as an ssh instance type.", action="store_true"),
    argument("--jupyter", help="Launch as a jupyter instance instead of an ssh instance.", action="store_true"),
    argument("--direct",  help="Use (faster) direct connections for jupyter & ssh.", action="store_true"),
    argument("--jupyter-dir", help="For runtype 'jupyter', directory in instance to use to launch jupyter. Defaults to image's working directory.", type=str),
    argument("--jupyter-lab", help="For runtype 'jupyter', Launch instance with jupyter lab.", action="store_true"),
    argument("--lang-utf8", help="Workaround for images with locale problems: install and generate locales before instance launch, and set locale to C.UTF-8.", action="store_true"),
    argument("--python-utf8", help="Workaround for images with locale problems: set python's locale to C.UTF-8.", action="store_true"),
    argument("--extra", help=argparse.SUPPRESS),
    argument("--env",   help="env variables and port mapping options, surround with '' ", type=str),
    argument("--args",  nargs=argparse.REMAINDER, help="list of arguments passed to container ENTRYPOINT. Onstart is recommended for this purpose."),
    argument("--create-from", help="Existing instance id to use as basis for new instance. Instance configuration should usually be identical, as only the difference from the base image is copied.", type=str),
    argument("--force", help="Skip sanity checks when creating from an existing instance", action="store_true"),
    usage="./vast launch --image image-name id command",
    help="Create instance, copy files, execute command, destroy instance.",
    epilog=deindent("""
        Examples:
         vast launch --image pytorch/pytorch 123456 "python -u myexperiment.py"
    """),
)
def launch(args: argparse.Namespace):
    r = _create_instance(args)
    r_props = r.json()
    instance_id = r_props['new_contract']
    print(f'Created instance {instance_id}.')
    try:
        _launch_job(args, instance_id)
    finally:
        args.id = instance_id
        _destroy_instance(args)

@parser.command(
    argument("id", help="id of instance to launch", type=int),
    argument("command", help="command to be run in the started instance", type=str),
    argument('--timeout', help="Maximum number of seconds to wait for instance to become available.", type=float,
             default=512.0),
    usage="./vast start run id command",
    help="Start an instance, install requirements, run command, and stop the instance.",
    epilog=deindent("""
        Examples:
         vast start run 123456 "python -u myexperiment.py"
    """),
)
def start__run(args: argparse.Namespace):
    instance_id = args.id
    _start_instance(args)
    print(f'Started instance {instance_id}.')
    try:
        _launch_job(args, instance_id)
    finally:
        _stop_instance(args)


@parser.command(
    argument("-t", "--type", default="on-demand", help="Show 'bid'(interruptible) or 'on-demand' offers. default: on-demand"),
    argument("-i", "--interruptible", dest="type", const="bid", action="store_const", help="Alias for --type=bid"),
    argument("-b", "--bid", dest="type", const="bid", action="store_const", help="Alias for --type=bid"),
    argument("-d", "--on-demand", dest="type", const="on-demand", action="store_const", help="Alias for --type=on-demand"),
    argument("-n", "--no-default", action="store_true", help="Disable default query"),
    argument("--disable-bundling", action="store_true", help="Show identical offers. This request is more heavily rate limited."),
    argument("--storage", type=float, default=5.0, help="Amount of storage to use for pricing, in GiB. default=5.0GiB"),
    argument("-o", "--order", type=str, help="Comma-separated list of fields to sort on. postfix field with - to sort desc. ex: -o 'num_gpus,total_flops-'.  default='score-'", default='score-'),
    argument("-f", "--fields", type=str, default=None, help="Comma-separated list of fields to show in the table."),
    argument("-m", "--max", type=int, default=None, help="Maximum number of table entries to show."),
    argument("query", help="Query to search for. default: 'external=false rentable=true verified=true', pass -n to ignore default", nargs="*", default=None),
    usage="./vast search offers [--help] [--api-key API_KEY] [--raw] <query>",
    help="Search for instance types using custom query",
    epilog=deindent("""
        Query syntax:

            query = comparison comparison...
            comparison = field op value
            field = <name of a field>
            op = one of: <, <=, ==, !=, >=, >, in, notin
            value = <bool, int, float, etc> | 'any'

        note: to pass '>' and '<' on the command line, make sure to use quotes
        note: to encode a string query value (ie for gpu_name), replace any spaces ' ' with underscore '_'


        Examples:

            ./vast search offers 'compute_cap > 610 total_flops < 5'
            ./vast search offers 'reliability > 0.99  num_gpus>=4' -o 'num_gpus-'
            ./vast search offers 'rentable = any'
            ./vast search offers 'reliability > 0.98 num_gpus=1 gpu_name=RTX_3090'

        Available fields:

              Name                  Type       Description

            bw_nvlink               float     bandwidth NVLink
            compute_cap:            int       cuda compute capability*100  (ie:  650 for 6.5, 700 for 7.0)
            cpu_cores:              int       # virtual cpus
            cpu_cores_effective:    float     # virtual cpus you get
            cpu_ram:                float     system RAM in gigabytes
            cuda_vers:              float     cuda version
            direct_port_count       int       open ports on host's router
            disk_bw:                float     disk read bandwidth, in MB/s
            disk_space:             float     disk storage space, in GB
            dlperf:                 float     DL-perf score  (see FAQ for explanation)
            dlperf_usd:             float     DL-perf/$
            dph:                    float     $/hour rental cost
            driver_version          string    driver version in use on a host.
            duration:               float     max rental duration in days
            external:               bool      show external offers in addition to datacenter offers
            flops_usd:              float     TFLOPs/$
            gpu_mem_bw:             float     GPU memory bandwidth in GB/s
            gpu_name:               string    GPU model name (no quotes, replace spaces with underscores, ie: RTX_3090 rather than 'RTX 3090')
            gpu_ram:                float     GPU RAM in GB
            gpu_frac:               float     Ratio of GPUs in the offer to gpus in the system
            has_avx:                bool      CPU supports AVX instruction set.
            id:                     int       instance unique ID
            inet_down:              float     internet download speed in Mb/s
            inet_down_cost:         float     internet download bandwidth cost in $/GB
            inet_up:                float     internet upload speed in Mb/s
            inet_up_cost:           float     internet upload bandwidth cost in $/GB
            machine_id              int       machine id of instance
            min_bid:                float     current minimum bid price in $/hr for interruptible
            num_gpus:               int       # of GPUs
            pci_gen:                float     PCIE generation
            pcie_bw:                float     PCIE bandwidth (CPU to GPU)
            reliability:            float     machine reliability score (see FAQ for explanation)
            rentable:               bool      is the instance currently rentable
            rented:                 bool      is the instance currently rented
            storage_cost:           float     storage cost in $/GB/month
            total_flops:            float     total TFLOPs from all GPUs
            verified:               bool      is the machine verified
    """),
    aliases=hidden_aliases(["search instances"]),
)
def search__offers(args):
    """Creates a query based on search parameters as in the examples above.

    :param argparse.Namespace args: should supply all the command-line options
    """
    field_alias = {
        "cuda_vers": "cuda_max_good",
        "reliability": "reliability2",
        "dlperf_usd": "dlperf_per_dphtotal",
        "dph": "dph_total",
        "flops_usd": "flops_per_dphtotal",
    }

    try:

        if args.no_default:
            query = {}
        else:
            query = {"verified": {"eq": True}, "external": {"eq": False}, "rentable": {"eq": True}}

        if args.query is not None:
            query = parse_query(args.query, query)

        order = []
        for name in args.order.split(","):
            name = name.strip()
            if not name:
                continue
            direction = "asc"
            if name.strip("-") != name:
                direction = "desc"
            field = name.strip("-")
            if field in field_alias:
                field = field_alias[field]
            order.append([field, direction])

        query["order"] = order
        query["type"] = args.type
        # For backwards compatibility, support --type=interruptible option
        if query["type"] == 'interruptible':
            query["type"] = 'bid'
        if args.disable_bundling:
            query["disable_bundling"] = True
    except ValueError as e:
        print("Error: ", e)
        return 1

    url = apiurl(args, "/bundles", {"q": query})
    r = requests.get(url)
    r.raise_for_status()
    rows = r.json()["offers"]
    if args.raw:
        print(json.dumps(rows, indent=1, sort_keys=True))
    else:
        shown_fields = displayable_fields
        if args.fields is not None:
            fields_set = set([f.strip() for f in args.fields.split(',')])
            shown_fields = [f for f in displayable_fields if f[0] in fields_set]
            if len(shown_fields) == 0:
                print('Error: Fields requested not found.')
                return 1
        display_table(rows, shown_fields, max_count=args.max)


@parser.command(
    usage="./vast show instances [--api-key API_KEY] [--raw]",
    help="Display user's current instances"
)
def show__instances(args):
    """
    Shows the stats on the machine the user is renting.

    :param argparse.Namespace args: should supply all the command-line options
    :rtype:
    """
    req_url = apiurl(args, "/instances", {"owner": "me"})
    r = requests.get(req_url)
    r.raise_for_status()
    rows = r.json()["instances"]
    if args.raw:
        print(json.dumps(rows, indent=1, sort_keys=True))
    else:
        display_table(rows, instance_fields)


@parser.command(
    argument("--id", help="id of instance", type=int),
    usage="./vast ssh-url",
    help="ssh url helper",
)
def ssh_url(args):
    """

    :param argparse.Namespace args: should supply all the command-line options
    :rtype:
    """
    return _ssh_url(args, "ssh://")


@parser.command(
    argument("--id", help="id of instance", type=int),
    usage="./vast scp-url",
    help="scp url helper",
)
def scp_url(args):
    """

    :param argparse.Namespace args: should supply all the command-line options
    :rtype:
    """
    return _ssh_url(args, "scp://")


def _ssh_url(args, protocol):
    req_url = apiurl(args, "/instances", {"owner": "me"})
    r = requests.get(req_url)
    r.raise_for_status()
    rows = r.json()["instances"]
    if args.id:
        instance, = [r for r in rows if r['id'] == args.id]
    elif len(rows) > 1:
        print("Found multiple running instances")
        return 1
    else:
        instance, = rows
    print(f'{protocol}root@{instance["ssh_host"]}:{instance["ssh_port"]}')


def _ssh_url_for_id(args, protocol, id: int):
    req_url = apiurl(args, "/instances", {"owner": "me"})
    r = requests.get(req_url)
    r.raise_for_status()
    rows = r.json()["instances"]
    instance, = [r for r in rows if r['id'] == id]
    return f'{protocol}root@{instance["ssh_host"]}:{instance["ssh_port"]}'


def _get_instance(args, target_id) -> typing.Any:
    req_url = apiurl(args, "/instances", {"owner": "me"}, add_rnd=True)
    r = requests.get(req_url)
    r.raise_for_status()
    rows = r.json()["instances"]
    instances = [r for r in rows if r['id'] == target_id]
    if len(instances) == 0:
        raise ValueError(f'No instances with ID {target_id}')
    return instances[0]


def _is_instance_running(args, target_id):
    instance = _get_instance(args, target_id)
    return instance['actual_status'] == 'running'


def _is_instance_obj_running(instance):
    return instance['actual_status'] == 'running'


def _wait_for_instance_running(args, target_id, timeout: float = 120):
    print(f'Waiting for instance {target_id}...')
    start_time = time.time()
    pause_time = 45.0
    while time.time() - start_time < timeout:
        instance = _get_instance(args, target_id)
        if _is_instance_obj_running(instance):
            return instance
        time.sleep(pause_time)
    raise TimeoutError(f'Instance did not start in {time.time() - start_time:.1f} seconds')


def _wait_for_connected_ssh_client(args, instance: any, timeout: float = 120):
    start_time = time.time()
    pause_time = 3.0
    while time.time() - start_time < timeout:
        try:
            client = _get_connected_ssh_client(args, instance)
            client.close()
            return
        except NoValidConnectionsError:
            time.sleep(pause_time)
    raise TimeoutError(f'Could not connect to instance in {time.time() - start_time:.1f} seconds')


def _ssh_host_port(args, target_id: int):
    instance = _get_instance(args, target_id)
    ssh_host = instance['ssh_host']
    ssh_port = instance['ssh_port']
    return ssh_host, ssh_port,


def _ssh_host_port_for_instance(instance: any):
    ssh_host = instance['ssh_host']
    ssh_port = instance['ssh_port']
    return ssh_host, ssh_port,


def _get_connected_ssh_client(args, instance: typing.Any):
    ssh_host, ssh_port = _ssh_host_port_for_instance(instance)
    ssh_client = paramiko.SSHClient()
    ssh_client.load_system_host_keys()
    ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh_client.connect(hostname=ssh_host, username='root', port=ssh_port)
    return ssh_client


def _relative_paths(src_path: str):
    file_paths = list(Path(src_path).rglob('*'))
    return [str(p.relative_to(src_path)) for p in file_paths]


def _scp_dowload(args, remote_path: str, local_path: str, instance: any, preserve_times: bool = True):
    ssh_host, ssh_port = _ssh_host_port_for_instance(instance)
    with paramiko.SSHClient() as ssh_client:
        ssh_client.load_system_host_keys()
        ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh_client.connect(hostname=ssh_host, username='root', port=ssh_port)
        with SCPClient(ssh_client.get_transport()) as scp:
            scp.get(remote_path, local_path, recursive=True, preserve_times=preserve_times)


def _upload_zip(instance: typing.Any, src_zip_path: str, remote_path: str):
    ssh_host, ssh_port = _ssh_host_port_for_instance(instance)
    file_size = os.path.getsize(src_zip_path)
    print(f'Uploading zip file of {file_size / 1e6:.4g} Mb...')
    with paramiko.SSHClient() as ssh_client:
        ssh_client.load_system_host_keys()
        ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh_client.connect(hostname=ssh_host, username='root', port=ssh_port)
        target_zip_path = '/tmp/vast-upload.zip'

        with tqdm(total=file_size, unit='B', unit_scale=True, desc="Uploading") as progress:
            def progress_fn(filename: bytes, size: int, sent: int, x):
                progress.update(sent - progress.n)

            try:
                with SCPClient(ssh_client.get_transport(), progress4=progress_fn) as scp:
                    scp.put([src_zip_path], target_zip_path)
            except SCPException as scp_err:
                print(f'Error: {scp_err}')
                return 1
        print('Installing zip package...')
        _execute(ssh_client, 'apt-get install -y zip')
        print('Unzipping uploaded files...')
        _execute(ssh_client, f'unzip -o {target_zip_path} -d {remote_path}')


def _scp_files(args, src_path: str, rel_src_paths: typing.List[str], instance: any, remote_path: str,
               check_gitignore: bool):
    gi_matches = None
    if check_gitignore:
        pattern_ignore_path = None
        vast_ignore_path = os.path.join(src_path, '.vastignore')
        if os.path.exists(vast_ignore_path):
            pattern_ignore_path = vast_ignore_path
        else:
            git_ignore_path = os.path.join(src_path, '.gitignore')
            if os.path.exists(git_ignore_path):
                pattern_ignore_path = git_ignore_path
        if pattern_ignore_path is not None:
            gi_matches = parse_gitignore(pattern_ignore_path)
    print('Building zip file...')
    tmp_zip_file = tempfile.NamedTemporaryFile(prefix='vast-', suffix='.zip', delete=False)
    try:
        count = 0
        with ZipFile(tmp_zip_file, 'w') as zip_object:
            # Adding files that need to be zipped
            for rel_path in rel_src_paths:
                ignore = gi_matches is not None and gi_matches(rel_path)
                if not ignore:
                    src_file_path = os.path.join(src_path, rel_path)
                    zip_object.write(src_file_path, arcname=rel_path)
                    count += 1
        print(f'Zipped {count} files. Excluded {len(rel_src_paths) - count}.')
        _upload_zip(instance, tmp_zip_file.name, remote_path)
    finally:
        try:
            os.remove(tmp_zip_file.name)
        except:
            # TODO ignoring issue
            pass


def _capture_channel_output(channel: Channel):
    some_receive = False
    second_attempt = False
    while True:
        time.sleep(0.001)
        if channel.recv_ready():
            solo_line = channel.recv(1024)
            sys.stdout.buffer.write(solo_line)
            sys.stdout.buffer.flush()
            some_receive = True
        elif channel.exit_status_ready():
            if some_receive or second_attempt:
                break
            second_attempt = True
            time.sleep(1.0)


def _execute(ssh_client: SSHClient, command: str, get_pty=False):
    stdout: ChannelFile
    stdin, stdout, stderr = ssh_client.exec_command(command, get_pty=get_pty)
    stdout.channel.set_combine_stderr(True)
    _capture_channel_output(stdout.channel)


def _send_command(c: Channel, text: str, verbose: bool = False):
    if not text.endswith('\n'):
        text = text + '\n'
    text_b = bytes(text, encoding='utf-8')
    while len(text_b) > 0:
        n_sent = c.send(text_b)
        if n_sent == 0:
            logging.warning('Got EOF sending shell command.')
            return
        text_b = text_b[n_sent:]
    if verbose:
        print(f'Shell: {text.strip()}')
        _capture_channel_output(c)


def _wait_for_instance_ready_timeout(args, instance_id: int):
    timeout = args.timeout
    time1 = time.time()
    instance = _wait_for_instance_running(args, instance_id, timeout=timeout)
    time2 = time.time()
    timeout -= (time2 - time1)
    _wait_for_connected_ssh_client(args, instance, timeout=timeout)
    return instance


def _launch_job_in_ready_instance(args, instance):
    src_path = '.'
    rel_src_paths = _relative_paths(src_path)
    has_reqs = 'requirements.txt' in rel_src_paths
    remote_path = _app_path
    _scp_files(args, src_path, rel_src_paths, instance, remote_path, check_gitignore=True)
    with _get_connected_ssh_client(args, instance) as ssh_client:
        path_script_file = '.vast-set-path.sh'
        path_script = f'{remote_path}/{path_script_file}'
        with ssh_client.invoke_shell() as shell:
            _send_command(shell, f'rm -f {path_script} && touch {path_script}\n')
            _send_command(shell, f'echo "export PYTHONUNBUFFERED=1" >> {path_script}\n')
            _send_command(shell, f'echo "export PYTHONPATH={remote_path}" >> {path_script}\n')
            _send_command(shell, f'echo "export PATH=\\"$PATH\\"" >> {path_script}\n')

        def _full_command(cmd: str) -> str:
            return f'cd {remote_path} && source {path_script_file} && {cmd}'

        if has_reqs:
            print('Installing requirements...')
            _execute(ssh_client, _full_command('pip install -r requirements.txt'))
        else:
            print('No requirements.txt file found.')
        print('===== command output follows =====')
        _execute(ssh_client, _full_command(args.command))
        print('===== end of command output ====')
        artifacts_remote_path = (Path(remote_path) / 'vast-artifacts').as_posix()
        try:
            _scp_dowload(args, artifacts_remote_path, src_path, instance)
            print('Artifacts downloaded.')
        except SCPException:
            print('No artifacts produced (or unable to download.)')


def _launch_job(args, instance_id: int):
    instance = _wait_for_instance_ready_timeout(args, instance_id)
    return _launch_job_in_ready_instance(args, instance)


def _start_instance(args):
    instance_id = args.id
    url = apiurl(args, "/instances/{id}/".format(id=instance_id))
    r = requests.put(url, json={
        "state": "running"
    })
    r.raise_for_status()
    instance = _wait_for_instance_ready_timeout(args, instance_id)
    return instance


def _stop_instance(args):
    url = apiurl(args, "/instances/{id}/".format(id=args.id))
    r = requests.put(url, json={
        "state": "stopped"
    })
    r.raise_for_status()


@parser.command(
    argument("-q", "--quiet", action="store_true", help="only display numeric ids"),
    usage="./vast show machines [OPTIONS]",
    help="[Host] Show hosted machines",
)
def show__machines(args):
    """
    Show the machines user is offering for rent.

    :param argparse.Namespace args: should supply all the command-line options
    :rtype:
    """
    req_url = apiurl(args, "/machines", {"owner": "me"});
    r = requests.get(req_url);
    r.raise_for_status()
    rows = r.json()["machines"]
    if args.raw:
        print(json.dumps(rows, indent=1, sort_keys=True))
    else:
        for machine in rows:
            if args.quiet:
                print("{id}".format(id=machine["id"]))
            else:
                print("{N} machines: ".format(N=len(rows)));
                print("{id}: {json}".format(id=machine["id"], json=json.dumps(machine, indent=4, sort_keys=True)))


@parser.command(
    argument("-q", "--quiet", action="store_true", help="only display numeric ids"),
    argument("-s", "--start_date", help="start date and time for report. Many formats accepted", type=str),
    argument("-e", "--end_date", help="end date and time for report. Many formats accepted ", type=str),
    argument("-m", "--machine_id", help="Machine id (optional)", type=int),
    usage="./vast show earnings [OPTIONS]",
    help="Get machine earning history reports",
)
def show__earnings(args):
    """
    Show earnings history for a time range, optionally per machine. Various options available to limit time range and type of items.

    :param argparse.Namespace args: should supply all the command-line options
    :rtype:
    """

    Minutes = 60.0;
    Hours	= 60.0*Minutes;
    Days	= 24.0*Hours;
    Years	= 365.0*Days;
    cday    = time.time() / Days
    sday = cday - 1.0
    eday = cday - 1.0

    try:
        import dateutil
        from dateutil import parser

    except ImportError:
        print("""\nWARNING: Missing dateutil, can't parse time format""")

    if args.end_date:
        try:
            end_date = dateutil.parser.parse(str(args.end_date))
            end_date_txt = end_date.isoformat()
            end_timestamp = time.mktime(end_date.timetuple())
            eday = end_timestamp / Days
        except ValueError:
            print("Warning: Invalid end date format! Ignoring end date!")

    if args.start_date:
        try:
            start_date = dateutil.parser.parse(str(args.start_date))
            start_date_txt = start_date.isoformat()
            start_timestamp = time.mktime(start_date.timetuple())
            sday = start_timestamp / Days
        except ValueError:
            print("Warning: Invalid start date format! Ignoring start date!")



    req_url = apiurl(args, "/users/me/machine-earnings", {"owner": "me", "sday": sday, "eday": eday, "machid" :args.machine_id});
    r = requests.get(req_url)
    r.raise_for_status()
    rows = r.json()

    print(json.dumps(rows, indent=1, sort_keys=True))



@parser.command(
    argument("-q", "--quiet", action="store_true", help="only display numeric ids"),
    argument("-s", "--start_date", help="start date and time for report. Many formats accepted (optional)", type=str),
    argument("-e", "--end_date", help="end date and time for report. Many formats accepted (optional)", type=str),
    argument("-c", "--only_charges", action="store_true", help="Show only charge items."),
    argument("-p", "--only_credits", action="store_true", help="Show only credit items."),
    usage="./vast show invoices [OPTIONS]",
    help="Get billing history reports",
)
def show__invoices(args):
    """
    Show current payments and charges. Various options available to limit time range and type
    of items. Default is to show everything for user's entire billing history.

    :param argparse.Namespace args: should supply all the command-line options
    :rtype:
    """
    req_url = apiurl(args, "/users/me/invoices", {"owner": "me", "inc_charges" : not args.only_credits});
    r = requests.get(req_url)
    r.raise_for_status()
    rows = r.json()["invoices"]
    # print("Timestamp for first row: ", rows[0]["timestamp"])
    invoice_filter_data = filter_invoice_items(args, rows)
    rows = invoice_filter_data["rows"]
    filter_header = invoice_filter_data["header_text"]

    current_charges = r.json()["current"]

    if args.raw:
        print(json.dumps(rows, indent=1, sort_keys=True))
        # print("Current: ", current_charges)
    else:
        print(filter_header)
        display_table(rows, invoice_fields)
        print("Current: ", current_charges)


@parser.command(
    argument("-q", "--quiet", action="store_true", help="display information about user"),
    usage="./vast show user [OPTIONS]",
    help="   Get current user data"
)
def show__user(args):
    """
    Shows stats for logged-in user. Does not show API key.

    :param argparse.Namespace args: should supply all the command-line options
    :rtype:
    """
    req_url = apiurl(args, "/users/current", {"owner": "me"});
    print(f"URL: {req_url}")
    print("HHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHHh\n")
    r = requests.get(req_url);
    r.raise_for_status()
    user_blob = r.json()
    user_blob.pop("api_key")

    if args.raw:
        print(json.dumps(user_blob, indent=1, sort_keys=True))
    else:
        display_table([user_blob], user_fields)


def filter_invoice_items(args: argparse.Namespace, rows: typing.List) -> typing.Dict:
    """This applies various filters to the invoice items. Currently it filters on start and end date and applies the
    'only_charge' and 'only_credits' options.invoice_number

    :param argparse.Namespace args: should supply all the command-line options
    :param List rows: The rows of items in the invoice

    :rtype List: Returns the filtered list of rows.

    """

    try:
        import vast_pdf
        import dateutil
        from dateutil import parser

    except ImportError:
        print("""\nWARNING: The 'vast_pdf' library is not present. This library is used to print invoices in PDF format. If
        you do not need this feature you can ignore this message. To get the library you should download the vast-python
        github repository. Just do 'git@github.com:vast-ai/vast-python.git' and then 'cd vast-python'. Once in that
        directory you can run 'vast.py' and it will have access to 'vast_pdf.py'. The library depends on a Python
        package called Borb to make the PDF files. To install this package do 'pip3 install borb'.\n""")

    try:
        vast_pdf
    except NameError:
        vast_pdf = Object()
        vast_pdf.invoice_number = -1


    selector_flag = ""
    end_timestamp: float = 9999999999
    start_timestamp: float = 0
    start_date_txt = ""
    end_date_txt = ""

    if args.end_date:
        try:
            end_date = dateutil.parser.parse(str(args.end_date))
            end_date_txt = end_date.isoformat()
            end_timestamp = time.mktime(end_date.timetuple())
        except ValueError:
            print("Warning: Invalid end date format! Ignoring end date!")
    if args.start_date:
        try:
            start_date = dateutil.parser.parse(str(args.start_date))
            start_date_txt = start_date.isoformat()
            start_timestamp = time.mktime(start_date.timetuple())
        except ValueError:
            print("Warning: Invalid start date format! Ignoring start date!")

    if args.only_charges:
        type_txt = "Only showing charges."
        selector_flag = "only_charges"

        def type_filter_fn(row):
            return True if row["type"] == "charge" else False
    elif args.only_credits:
        type_txt = "Only showing credits."
        selector_flag = "only_credits"

        def type_filter_fn(row):
            return True if row["type"] == "payment" else False
    else:
        type_txt = ""

        def type_filter_fn(row):
            return True

    if args.end_date:
        if args.start_date:
            header_text = f'Invoice items after {start_date_txt} and before {end_date_txt}.'
        else:
            header_text = f'Invoice items before {end_date_txt}.'
    elif args.start_date:
        header_text = f'Invoice items after {start_date_txt}.'
    else:
        header_text = " "

    header_text = header_text + " " + type_txt

    rows = list(filter(lambda row: end_timestamp >= (row["timestamp"] or 0.0) >= start_timestamp and type_filter_fn(row) and float(row["amount"]) != 0, rows))

    if start_date_txt:
        start_date_txt = "S:" + start_date_txt

    if end_date_txt:
        end_date_txt = "E:" + end_date_txt

    now = date.today()
    invoice_number: int = now.year * 12 + now.month - 1


    pdf_filename_fields = list(filter(lambda fld: False if fld == "" else True,
                                      [str(invoice_number),
                                       start_date_txt,
                                       end_date_txt,
                                       selector_flag]))

    filename = "invoice_" + "-".join(pdf_filename_fields) + ".pdf"
    return {"rows": rows, "header_text": header_text, "pdf_filename": filename}


@parser.command(
    argument("-q", "--quiet", action="store_true", help="only display numeric ids"),
    argument("-s", "--start_date", help="start date and time for report. Many formats accepted (optional)", type=str),
    argument("-e", "--end_date", help="end date and time for report. Many formats accepted (optional)", type=str),
    argument("-c", "--only_charges", action="store_true", help="Show only charge items."),
    argument("-p", "--only_credits", action="store_true", help="Show only credit items."),
    usage="./vast generate pdf_invoices [OPTIONS]",
)
def generate__pdf_invoices(args):
    """
    Makes a PDF version of the data returned by the "show invoices" command. Takes the same command line args as that
    command.

    :param argparse.Namespace args: should supply all the command-line options
    :rtype:
    """

    try:
        import vast_pdf
    except ImportError:
        print("""\nWARNING: The 'vast_pdf' library is not present. This library is used to print invoices in PDF format. If
        you do not need this feature you can ignore this message. To get the library you should download the vast-python
        github repository. Just do 'git@github.com:vast-ai/vast-python.git' and then 'cd vast-python'. Once in that
        directory you can run 'vast.py' and it will have access to 'vast_pdf.py'. The library depends on a Python
        package called Borb to make the PDF files. To install this package do 'pip3 install borb'.\n""")

    req_url_inv = apiurl(args, "/users/me/invoices", {"owner": "me"})
    r_inv = requests.get(req_url_inv)
    r_inv.raise_for_status()
    rows_inv = r_inv.json()["invoices"]
    invoice_filter_data = filter_invoice_items(args, rows_inv)
    rows_inv = invoice_filter_data["rows"]
    req_url = apiurl(args, "/users/current", {"owner": "me"})
    r = requests.get(req_url)
    r.raise_for_status()
    user_blob = r.json()
    user_blob = translate_null_strings_to_blanks(user_blob)

    if args.raw:
        print(json.dumps(rows_inv, indent=1, sort_keys=True))
        print("Current: ", user_blob)
        print("Raw mode")
    else:
        display_table(rows_inv, invoice_fields)
        vast_pdf.generate_invoice(user_blob, rows_inv, invoice_filter_data)


@parser.command(
    argument("id", help="id of machine to list", type=int),
    argument("-g", "--price_gpu", help="per gpu rental price in $/hour  (price for active instances)", type=float),
    argument("-s", "--price_disk",
             help="storage price in $/GB/month (price for inactive instances), default: $0.15/GB/month", type=float),
    argument("-u", "--price_inetu", help="price for internet upload bandwidth in $/GB", type=float),
    argument("-d", "--price_inetd", help="price for internet download bandwidth in $/GB", type=float),
    argument("-m", "--min_chunk", help="minimum amount of gpus", type=int),
    argument("-e", "--end_date", help="unix timestamp of the available until date (optional)", type=int),
    usage="./vast list machine id [--price_gpu PRICE_GPU] [--price_inetu PRICE_INETU] [--price_inetd PRICE_INETD] [--api-key API_KEY]",
    help="[Host] list a machine for rent",
)
def list__machine(args):
    """


    :param argparse.Namespace args: should supply all the command-line options
    :rtype:
    """
    req_url = apiurl(args, "/machines/create_asks/")
    r = requests.put(req_url, json={'machine': args.id, 'price_gpu': args.price_gpu,
                                    'price_disk': args.price_disk, 'price_inetu': args.price_inetu,
                                    'price_inetd': args.price_inetd, 'min_chunk': args.min_chunk,
                                    'end_date': args.end_date})

    if (r.status_code == 200):
        rj = r.json();
        if (rj["success"]):
            price_gpu_ = str(args.price_gpu) if args.price_gpu is not None else "def";
            price_inetu_ = str(args.price_inetu);
            price_inetd_ = str(args.price_inetd);
            min_chunk_ = str(args.min_chunk);
            end_date_ = str(args.end_date);
            print(
                "offers created for machine {args.id},  @ ${price_gpu_}/gpu/day, ${price_inetu_}/GB up, ${price_inetd_}/GB down, {min_chunk_}/min gpus, till {end_date_}".format(
                    **locals()));
        else:
            print(rj["msg"]);
    else:
        print(r.text);
        print("failed with error {r.status_code}".format(**locals()));


@parser.command(
    argument("id", help="id of machine to unlist", type=int),
    usage="./vast unlist machine <id>",
    help="[Host] Unlist a listed machine",
)
def unlist__machine(args):
    """
    Removes machine from list of machines for rent.

    :param argparse.Namespace args: should supply all the command-line options
    :rtype:
    """
    req_url = apiurl(args, "/machines/{machine_id}/asks/".format(machine_id=args.id));
    r = requests.delete(req_url)
    if (r.status_code == 200):
        rj = r.json();
        if (rj["success"]):
            print("all offers for machine {machine_id} removed, machine delisted.".format(machine_id=args.id));
        else:
            print(rj["msg"]);
    else:
        print(r.text);
        print("failed with error {r.status_code}".format(**locals()));


@parser.command(
    argument("id", help="id of machine to remove default instance from", type=int),
    help="[Host] Delete default jobs",
)
def remove__defjob(args):
    """


    :param argparse.Namespace args: should supply all the command-line options
    :rtype:
    """
    req_url = apiurl(args, "/machines/{machine_id}/defjob/".format(machine_id=args.id));
    # print(req_url);
    r = requests.delete(req_url);

    if (r.status_code == 200):
        rj = r.json();
        if (rj["success"]):
            print("default instance for machine {machine_id} removed.".format(machine_id=args.id));
        else:
            print(rj["msg"]);
    else:
        print(r.text);
        print("failed with error {r.status_code}".format(**locals()));


def set_ask(args):
    """

    :param argparse.Namespace args: should supply all the command-line options
    :rtype:
    """
    print("set asks!\n");


@parser.command(
    argument("id", help="id of instance to reboot", type=int),
    usage="./vast reboot instance <id> [--raw]",
    help="Reboot (stop/start) an instance",
)
def reboot__instance(args):
    """
    :param argparse.Namespace args: should supply all the command-line options
    :rtype:
    """
    url = apiurl(args, "/instances/reboot/{id}/".format(id=args.id))
    r = requests.put(url, json={})
    r.raise_for_status()

    if (r.status_code == 200):
        rj = r.json();
        if (rj["success"]):
            print("Rebooting instance {args.id}.".format(**(locals())));
        else:
            print(rj["msg"]);
    else:
        print(r.text);
        print("failed with error {r.status_code}".format(**locals()));


@parser.command(
    argument("id", help="id of instance to start/restart", type=int),
    usage="./vast start instance <id> [--raw]",
    help="Start a stopped instance",
)
def start__instance(args):
    """

    :param argparse.Namespace args: should supply all the command-line options
    :rtype:
    """
    url = apiurl(args, "/instances/{id}/".format(id=args.id))
    r = requests.put(url, json={
        "state": "running"
    })
    r.raise_for_status()

    if (r.status_code == 200):
        rj = r.json();
        if (rj["success"]):
            print("starting instance {args.id}.".format(**(locals())));
        else:
            print(rj["msg"]);
    else:
        print(r.text);
        print("failed with error {r.status_code}".format(**locals()));


@parser.command(
    argument("id", help="id of instance to stop", type=int),
    usage="./vast stop instance [--raw] <id>",
    help="Stop a running instance",
)
def stop__instance(args):
    """

    :param argparse.Namespace args: should supply all the command-line options
    :rtype:
    """
    url = apiurl(args, "/instances/{id}/".format(id=args.id))
    r = requests.put(url, json={
        "state": "stopped"
    })
    r.raise_for_status()

    if (r.status_code == 200):
        rj = r.json();
        if (rj["success"]):
            print("stopping instance {args.id}.".format(**(locals())));
        else:
            print(rj["msg"]);
    else:
        print(r.text);
        print("failed with error {r.status_code}".format(**locals()));


@parser.command(
    argument("id", help="id of instance to label", type=int),
    argument("label", help="label to set", type=str),
    usage="./vast label instance <id> <label>",
    help="Assign a string label to an instance",
)
def label__instance(args):
    """

    :param argparse.Namespace args: should supply all the command-line options
    :rtype:
    """
    url = apiurl(args, "/instances/{id}/".format(id=args.id))
    r = requests.put(url, json={
        "label": args.label
    })
    r.raise_for_status()

    rj = r.json();
    if rj["success"]:
        print("label for {args.id} set to {args.label}.".format(**(locals())));
    else:
        print(rj["msg"]);


@parser.command(
    argument("id", help="id of instance to delete", type=int),
    usage="./vast destroy instance id [-h] [--api-key API_KEY] [--raw]",
    help="Destroy an instance (irreversible, deletes data)",
)
def destroy__instance(args):
    """Perfoms the same action as pressing the "DESTROY" button on the website at https://console.vast.ai/instances/.

    :param argparse.Namespace args: should supply all the command-line options
    """
    r = _destroy_instance(args)
    if (r.status_code == 200):
        rj = r.json();
        if (rj["success"]):
            print("destroying instance {args.id}.".format(**(locals())));
        else:
            print(rj["msg"]);
    else:
        print(r.text);
        print("failed with error {r.status_code}".format(**locals()));




@parser.command(
    argument("ID", help="id of instance to execute on", type=int),
    argument("COMMAND", help="bash command surrounded by single quotes",  type=str),
    usage="./vast execute ID COMMAND",
    help="Execute a (constrained) remote command on a machine",
    epilog=deindent("""
        examples:
          ./vast execute 99999 'ls -l -o -r'
          ./vast execute 99999 'rm -r home/delete_this.txt'
          ./vast execute 99999 'du -d2 -h'

        available commands:
          ls                 List directory contents
          rm                 Remote files or directories
          du                 Summarize device usage for a set of files


    """),
)
def execute(args):
    """Execute a (constrained) remote command on a machine.
    :param argparse.Namespace args: should supply all the command-line options
    """
    url = apiurl(args, "/instances/command/{id}/".format(id=args.ID))
    r = requests.put(url, json={"command": args.COMMAND} )
    r.raise_for_status()

    if (r.status_code == 200):
        rj = r.json();
        if (rj["success"]):
            for i in range(0,30):
                time.sleep(0.3)
                #url = args.url + "/static/docker_logs/C" + str(args.ID&255) + ".log" # apiurl(args, "/instances/request_logs/{id}/".format(id=args.id))
                api_key_id_h = hashlib.md5( (args.api_key + str(args.ID)).encode('utf-8') ).hexdigest()
                #url = "https://s3.amazonaws.com/vast.ai/instance_logs/" + args.api_key + str(args.ID) + "C.log"
                url = "https://s3.amazonaws.com/vast.ai/instance_logs/" + api_key_id_h + "C.log"
                #print(url)
                r = requests.get(url);
                if (r.status_code == 200):
                    filtered_text = r.text.replace(rj["writeable_path"], '');
                    print(filtered_text)
                    break
        else:
            print(rj);
    else:
        print(r.text);
        print("failed with error {r.status_code}".format(**locals()));



@parser.command(
    argument("INSTANCE_ID", help="id of instance", type=int),
    argument("--tail", help="Number of lines to show from the end of the logs (default '1000')", type=str),
    usage="./vast logs [OPTIONS] INSTANCE_ID",
    help="Get the logs for an instance",
)
def logs(args):
    """Get the logs for an instance
    :param argparse.Namespace args: should supply all the command-line options
    """
    url = apiurl(args, "/instances/request_logs/{id}/".format(id=args.INSTANCE_ID))
    #url = apiurl(args, "/instances/bid_price/{id}/".format(id=args.INSTANCE_ID))
    json = {}
    if (args.tail):
        json['tail'] = args.tail
    r = requests.put(url, json=json )
    r.raise_for_status()

    if (r.status_code == 200):
        rj = r.json();
        for i in range(0,30):
            time.sleep(0.3)
            #url = args.url + "/static/docker_logs/C" + str(args.INSTANCE_ID&255) + ".log" # apiurl(args, "/instances/request_logs/{id}/".format(id=args.id))
            #url = "https://s3.amazonaws.com/vast.ai/instance_logs/" + args.api_key + str(args.INSTANCE_ID) + ".log"
            api_key_id_h = hashlib.md5( (args.api_key + str(args.INSTANCE_ID)).encode('utf-8') ).hexdigest()
            url = "https://s3.amazonaws.com/vast.ai/instance_logs/" + api_key_id_h + ".log"
            print(f"waiting on logs for instance {args.INSTANCE_ID} fetching from {url}")
            r = requests.get(url);
            if (r.status_code == 200):
                print(r.text)
                break
        else:
            print(rj["msg"]);
    else:
        print(r.text);
        print("failed with error {r.status_code}".format(**locals()));


@parser.command(
    argument("id", help="id of machine to launch default instance on", type=int),
    argument("--price_gpu", help="per gpu rental price in $/hour", type=float),
    argument("--price_inetu", help="price for internet upload bandwidth in $/GB", type=float),
    argument("--price_inetd", help="price for internet download bandwidth in $/GB", type=float),
    argument("--image", help="docker container image to launch", type=str),
    argument("--args", nargs=argparse.REMAINDER, help="list of arguments passed to container launch"),
    usage="./vast set defjob id [--api-key API_KEY] [--price_gpu PRICE_GPU] [--price_inetu PRICE_INETU] [--price_inetd PRICE_INETD] [--image IMAGE] [--args ...]",
    help="[Host] Create default jobs for a machine",
)
def set__defjob(args):
    """

    :param argparse.Namespace args: should supply all the command-line options
    :rtype:
    """
    req_url = apiurl(args, "/machines/create_bids/");
    print(f"URL:{req_url}")
    r = requests.put(req_url, json={'machine': args.id, 'price_gpu': args.price_gpu, 'price_inetu': args.price_inetu, 'price_inetd': args.price_inetd, 'image': args.image, 'args': args.args});

    if (r.status_code == 200):

        rj = r.json();
        if (rj["success"]):
            print(
                "bids created for machine {args.id},  @ ${args.price_gpu}/gpu/day, ${args.price_inetu}/GB up, ${args.price_inetd}/GB down".format(
                    **locals()));
        else:
            print(rj["msg"]);
    else:
        print(r.text);
        print("failed with error {r.status_code}".format(**locals()));


def parse_env(envs):
    result = {}
    if (envs is None):
        return result
    env  = envs.split(' ')
    prev = None
    for e in env:
        if (prev is None):
          if ((e == "-e") or (e == "-p")):
              prev = e
          else:
              return result
        else:
          if (prev == "-p"):
            if set(e).issubset(set("0123456789:")):
                result["-p " + e] = "1"
            else:
                return result
          elif (prev == "-e"):
            e = e.strip(" '\"")
            if True: #set(e).issubset(set("1234567890abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_=")):
                kv = e.split('=')
                result[kv[0]] = kv[1]
            else:
                return result
          prev = None
    return result


#print(parse_env("-e TYZ=BM3828 -e BOB=UTC -p 10831:22 -p 8080:8080"))

def _create_instance(args):
    if args.onstart:
        with open(args.onstart, "r") as reader:
            args.onstart_cmd = reader.read()
    runtype = 'ssh'
    if args.args:
        runtype = 'args'
    if args.jupyter_dir or args.jupyter_lab:
        args.jupyter = True
    if args.jupyter and runtype == 'args':
        print("Error: Can't use --jupyter and --args together. Try --onstart or --onstart-cmd instead of --args.", file=sys.stderr)
        return 1

    if args.jupyter:
        runtype = 'jupyter_direc ssh_direct ssh_proxy' if args.direct else 'jupyter_proxy ssh_proxy'

    if args.ssh:
        runtype = 'ssh_direct ssh_proxy' if args.direct else 'ssh_proxy'

    url = apiurl(args, "/asks/{id}/".format(id=args.id))
    r = requests.put(url, json={
        "client_id": "me",
        "image": args.image,
        "args": args.args,
        "env": parse_env(args.env),
        "price": args.price,
        "disk": args.disk,
        "label": args.label,
        "extra": args.extra,
        "onstart": args.onstart_cmd,
        "runtype": runtype,
        "image_login": args.login,
        "python_utf8": args.python_utf8,
        "lang_utf8": args.lang_utf8,
        "use_jupyter_lab": args.jupyter_lab,
        "jupyter_dir": args.jupyter_dir,
        "create_from": args.create_from,
        "force": args.force
    })
    r.raise_for_status()
    return r


def _destroy_instance(args):
    url = apiurl(args, "/instances/{id}/".format(id=args.id))
    r = requests.delete(url, json={})
    r.raise_for_status()
    return r


@parser.command(
    argument("id", help="id of instance type to launch", type=int),
    argument("--price", help="per machine bid price in $/hour", type=float),
    argument("--disk", help="size of local disk partition in GB", type=float, default=10),
    argument("--image", help="docker container image to launch", type=str),
    argument("--login", help="docker login arguments for private repo authentication, surround with '' ", type=str),
    argument("--label", help="label to set on the instance", type=str),
    argument("--onstart", help="filename to use as onstart script", type=str),
    argument("--onstart-cmd", help="contents of onstart script as single argument", type=str),
    argument("--ssh",     help="Launch as an ssh instance type.", action="store_true"),
    argument("--jupyter", help="Launch as a jupyter instance instead of an ssh instance.", action="store_true"),
    argument("--direct",  help="Use (faster) direct connections for jupyter & ssh.", action="store_true"),
    argument("--jupyter-dir", help="For runtype 'jupyter', directory in instance to use to launch jupyter. Defaults to image's working directory.", type=str),
    argument("--jupyter-lab", help="For runtype 'jupyter', Launch instance with jupyter lab.", action="store_true"),
    argument("--lang-utf8", help="Workaround for images with locale problems: install and generate locales before instance launch, and set locale to C.UTF-8.", action="store_true"),
    argument("--python-utf8", help="Workaround for images with locale problems: set python's locale to C.UTF-8.", action="store_true"),
    argument("--extra", help=argparse.SUPPRESS),
    argument("--env",   help="env variables and port mapping options, surround with '' ", type=str),
    argument("--args",  nargs=argparse.REMAINDER, help="list of arguments passed to container ENTRYPOINT. Onstart is recommended for this purpose."),
    argument("--create-from", help="Existing instance id to use as basis for new instance. Instance configuration should usually be identical, as only the difference from the base image is copied.", type=str),
    argument("--force", help="Skip sanity checks when creating from an existing instance", action="store_true"),
    usage="./vast create instance id [OPTIONS] [--args ...]",
    help="Create a new instance",
    epilog=deindent("""
        Examples:
        vast create instance 384827 --image bobsrepo/pytorch:latest --login '-u bob -p 9d8df!fd89ufZ docker.io' --jupyter --direct --env '-e TZ=PDT -e XNAME=XX4 -p 22:22 -p 8080:8080' --disk 20
        vast create instance 344521 --image anthonytatowicz/eth-cuda-miner --disk 20 --args -U -S us-west1.nanopool.org:9999 -O 0x5C9314b28Fbf25D1d054a9184C0b6abF27E20d95 --farm-recheck 200
    """),
)
def create__instance(args: argparse.Namespace):
    """Performs the same action as pressing the "RENT" button on the website at https://console.vast.ai/create/.

    :param argparse.Namespace args: Namespace with many fields relevant to the endpoint.
    """
    r = _create_instance(args)
    if args.raw:
        print(json.dumps(r.json(), indent=1))
    else:
        print("Started. {}".format(r.json()))


@parser.command(
    argument("id", help="id of instance type to change bid", type=int),
    argument("--price", help="per machine bid price in $/hour", type=float),
    usage="./vast change bid id [--price PRICE]",
    help="Change the bid price for a spot/interruptible instance",
    epilog=deindent("""
        Change the current bid price of instance id to PRICE.
        If PRICE is not specified, then a winning bid price is used as the default.
    """),
)
def change__bid(args: argparse.Namespace):
    """Alter the bid with id contained in args.

    :param argparse.Namespace args: should supply all the command-line options
    :rtype int:
    """
    url = apiurl(args, "/instances/bid_price/{id}/".format(id=args.id))
    print(f"URL: {url}")
    r = requests.put(url, json={
        "client_id": "me",
        "price": args.price,
    })
    r.raise_for_status()
    print("Per gpu bid price changed".format(r.json()))





def pretty_print_POST(req):
    print('{}\n{}\r\n{}\r\n\r\n{}'.format(
        '-----------START-----------',
        req.method + ' ' + req.url,
        '\r\n'.join('{}: {}'.format(k, v) for k, v in req.headers.items()),
        req.body,
    ))


@parser.command(
    argument("id", help="id of machine to set min bid price for", type=int),
    argument("--price", help="per gpu min bid price in $/hour", type=float),
    usage="./vast set min_bid id [--price PRICE]",
    help="[Host] Set the minimum bid/rental price for a machine",
    epilog=deindent("""
        Change the current min bid price of machine id to PRICE.
    """),
)
def set__min_bid(args):
    """

    :param argparse.Namespace args: should supply all the command-line options
    :rtype:
    """
    url = apiurl(args, "/machines/{id}/minbid/".format(id=args.id))
    print(url)

    req = requests.put(url, json={"client_id": "me", "price": args.price,})
    #prepared = req.prepare()
    #pretty_print_POST(prepared)

    r = requests.put(url, json={"client_id": "me", "price": args.price,})

    print(r.request.url)
    print(r.request.body)
    print(r.request.headers)

    r.raise_for_status()
    print("Per gpu min bid price changed".format(r.json()))



@parser.command(
    usage="./vast reset api-key",
    help="Reset your api-key (get new key from website).",
)
def reset__api_key(args):
    """Caution: a bad API key will make it impossible to connect to the servers.
    """
    print('fml')
    #url = apiurl(args, "/users/current/reset-apikey/", {"owner": "me"})
    url = apiurl(args, "/commands/reset_apikey/" )
    r = requests.put(url, json={"client_id": "me",})
    r.raise_for_status()
    print("api-key reset ".format(r.json()))


@parser.command(
    argument("new_api_key", help="Api key to set as currently logged in user"),
    usage="./vast set api-key APIKEY",
    help="Set api-key (get your api-key from the console/CLI)",
)
def set__api_key(args):
    """Caution: a bad API key will make it impossible to connect to the servers.

    :param argparse.Namespace args: should supply all the command-line options
    """
    with open(api_key_file, "w") as writer:
        writer.write(args.new_api_key)
    print("Your api key has been saved in {}".format(api_key_file_base))


login_deprecated_message = """
login via the command line is no longer supported.
go to https://console.vast.ai/cli in a web browser to get your api key, then run:

    vast set api-key YOUR_API_KEY_HERE
"""

"""
@parser.command(
    argument("ignored", nargs="*"),
    usage=login_deprecated_message
)
def create__account(args):
    print(login_deprecated_message)

@parser.command(
    argument("ignored", nargs="*"),
    usage=login_deprecated_message,
)
def login(args):
    print(login_deprecated_message)
"""

def main():
    parser.add_argument("--url", help="server REST api url", default=server_url_default)
    parser.add_argument("--raw", action="store_true", help="output machine-readable json");
    parser.add_argument("--api-key", help="api key. defaults to using the one stored in {}".format(api_key_file_base), type=str, required=False, default=api_key_guard)


    args = parser.parse_args()
    if args.api_key is api_key_guard:
        if os.path.exists(api_key_file):
            with open(api_key_file, "r") as reader:
                args.api_key = reader.read().strip()
        else:
            args.api_key = None
    try:
        sys.exit(args.func(args) or 0)
    except requests.exceptions.HTTPError as e:
        try:
            errmsg = e.response.json().get("msg");
        except JSONDecodeError:
            if e.response.status_code == 401:
                errmsg = "Please log in or sign up"
            else:
                errmsg = "(no detail message supplied)"
        print("failed with error {e.response.status_code}: {errmsg}".format(**locals()));


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, BrokenPipeError):
        pass
