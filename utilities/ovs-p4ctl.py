#! /usr/bin/python3
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at:
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
ovs-p4ctl utility allows to control P4 bridges.
"""

import argparse
import codecs
import sys
import grpc
import logging
import json
import math
import ovspy.client
import queue
import re
import socket
import threading
import time
from functools import wraps

import google.protobuf.text_format
from google.rpc import status_pb2, code_pb2

from p4.v1 import p4runtime_pb2
from p4.v1 import p4runtime_pb2_grpc
from p4.config.v1 import p4info_pb2

# context = Context()

USAGE = "ovs-p4ctl: P4Runtime switch management utility\n" \
        "usage: ovs-p4ctl [OPTIONS] COMMAND [ARG...]\n" \
        "\nFor P4Runtime switches:\n" \
        "  show SWITCH                 show P4Runtime switch information\n" \
        "  set-pipe SWITCH PROGRAM P4INFO  set P4 pipeline for the swtich\n" \
        "  get-pipe SWITCH             get current P4 pipeline (P4Info) and print it\n" \
        "  dump-tables SWITCH          print table stats\n" \
        "  dump-table SWITCH TABLE     print table information\n" \
        "  add-entry SWITCH TABLE MATCH_KEY ACTION ACTION_DATA  adds new table entry\n"

def usage():
    print(USAGE)
    sys.exit(0)


class P4RuntimeErrorFormatException(Exception):
    def __init__(self, message):
        super().__init__(message)


# Used to iterate over the p4.Error messages in a gRPC error Status object
class P4RuntimeErrorIterator:
    def __init__(self, grpc_error):
        assert(grpc_error.code() == grpc.StatusCode.UNKNOWN)
        self.grpc_error = grpc_error

        error = None
        # The gRPC Python package does not have a convenient way to access the
        # binary details for the error: they are treated as trailing metadata.
        for meta in self.grpc_error.trailing_metadata():
            if meta[0] == "grpc-status-details-bin":
                error = status_pb2.Status()
                error.ParseFromString(meta[1])
                break
        if error is None:
            raise P4RuntimeErrorFormatException("No binary details field")

        if len(error.details) == 0:
            raise P4RuntimeErrorFormatException(
                "Binary details field has empty Any details repeated field")
        self.errors = error.details
        self.idx = 0

    def __iter__(self):
        return self

    def __next__(self):
        while self.idx < len(self.errors):
            p4_error = p4runtime_pb2.Error()
            one_error_any = self.errors[self.idx]
            if not one_error_any.Unpack(p4_error):
                raise P4RuntimeErrorFormatException(
                    "Cannot convert Any message to p4.Error")
            if p4_error.canonical_code == code_pb2.OK:
                continue
            v = self.idx, p4_error
            self.idx += 1
            return v
        raise StopIteration


class P4RuntimeWriteException(Exception):
    def __init__(self, grpc_error):
        assert(grpc_error.code() == grpc.StatusCode.UNKNOWN)
        super().__init__()
        self.errors = []
        try:
            error_iterator = P4RuntimeErrorIterator(grpc_error)
            for error_tuple in error_iterator:
                self.errors.append(error_tuple)
        except P4RuntimeErrorFormatException:
            raise  # just propagate exception for now

    def __str__(self):
        message = "Error(s) during Write:\n"
        for idx, p4_error in self.errors:
            code_name = code_pb2._CODE.values_by_number[
                p4_error.canonical_code].name
            message += "\t* At index {}: {}, '{}'\n".format(
                idx, code_name, p4_error.message)
        return message


class P4RuntimeException(Exception):
    def __init__(self, grpc_error):
        super().__init__()
        self.grpc_error = grpc_error

    def __str__(self):
        message = "P4Runtime RPC error ({}): {}".format(
            self.grpc_error.code().name, self.grpc_error.details())
        return message

def parse_p4runtime_write_error(f):
    @wraps(f)
    def handle(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except grpc.RpcError as e:
            if e.code() != grpc.StatusCode.UNKNOWN:
                raise e
            raise P4RuntimeWriteException(e) from None
    return handle


def parse_p4runtime_error(f):
    @wraps(f)
    def handle(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except grpc.RpcError as e:
            raise P4RuntimeException(e) from None
    return handle

mac_pattern = re.compile('^([\da-fA-F]{2}:){5}([\da-fA-F]{2})$')
def matchesMac(mac_addr_string):
    return mac_pattern.match(mac_addr_string) is not None

def encodeMac(mac_addr_string):
    str = mac_addr_string.replace(':', '')
    return codecs.decode(str, 'hex_codec')

def decodeMac(encoded_mac_addr):
    return ':'.join(s.encode('hex') for s in encoded_mac_addr)

ip_pattern = re.compile('^(\d{1,3}\.){3}(\d{1,3})$')
def matchesIPv4(ip_addr_string):
    return ip_pattern.match(ip_addr_string) is not None

def encodeIPv4(ip_addr_string):
    return socket.inet_aton(ip_addr_string)

def decodeIPv4(encoded_ip_addr):
    return socket.inet_ntoa(encoded_ip_addr)

def bitwidthToBytes(bitwidth):
    return int(math.ceil(bitwidth / 8.0))

def encodeNum(number, bitwidth):
    byte_len = bitwidthToBytes(bitwidth)
    num_str = '%x' % number
    if number >= 2 ** bitwidth:
        raise Exception("Number, %d, does not fit in %d bits" % (number, bitwidth))
    val = ('0' * (byte_len * 2 - len(num_str)) + num_str)
    return codecs.decode(val, 'hex_codec')

def decodeNum(encoded_number):
    return int(codecs.encode(encoded_number, 'hex_codec'), 16)

def encode(x, bitwidth):
    'Tries to infer the type of `x` and encode it'
    byte_len = bitwidthToBytes(bitwidth)
    if (type(x) == list or type(x) == tuple) and len(x) == 1:
        x = x[0]
    encoded_bytes = None
    if type(x) == str:
        if matchesMac(x):
            encoded_bytes = encodeMac(x)
        elif matchesIPv4(x):
            encoded_bytes = encodeIPv4(x)
        else:
            # Assume that the string is already encoded
            encoded_bytes = x
    elif type(x) == int:
        encoded_bytes = encodeNum(x, bitwidth)
    else:
        raise Exception("Encoding objects of %r is not supported" % type(x))
    assert(len(encoded_bytes) == byte_len)
    return encoded_bytes

class P4InfoHelper(object):
    def __init__(self, p4info):
        self.p4info = p4info

    def get(self, entity_type, name=None, id=None):
        if name is not None and id is not None:
            raise AssertionError("name or id must be None")

        for o in getattr(self.p4info, entity_type):
            pre = o.preamble
            if name:
                if (pre.name == name or pre.alias == name):
                    return o
            else:
                if pre.id == id:
                    return o

        if name:
            raise AttributeError("Could not find %r of type %s" % (name, entity_type))
        else:
            raise AttributeError("Could not find id %r of type %s" % (id, entity_type))

    def get_id(self, entity_type, name):
        return self.get(entity_type, name=name).preamble.id

    def get_name(self, entity_type, id):
        return self.get(entity_type, id=id).preamble.name

    def get_alias(self, entity_type, id):
        return self.get(entity_type, id=id).preamble.alias

    def __getattr__(self, attr):
        # Synthesize convenience functions for name to id lookups for top-level entities
        # e.g. get_tables_id(name_string) or get_actions_id(name_string)
        m = re.search("^get_(\w+)_id$", attr)
        if m:
            primitive = m.group(1)
            return lambda name: self.get_id(primitive, name)

        # Synthesize convenience functions for id to name lookups
        # e.g. get_tables_name(id) or get_actions_name(id)
        m = re.search("^get_(\w+)_name$", attr)
        if m:
            primitive = m.group(1)
            return lambda id: self.get_name(primitive, id)

        raise AttributeError("%r object has no attribute %r" % (self.__class__, attr))

    def get_match_field(self, table_name, name=None, id=None):
        for t in self.p4info.tables:
            pre = t.preamble
            if pre.name == table_name:
                for mf in t.match_fields:
                    if name is not None:
                        if mf.name == name:
                            return mf
                    elif id is not None:
                        if mf.id == id:
                            return mf
        raise AttributeError("%r has no attribute %r" % (table_name, name if name is not None else id))

    def get_match_field_id(self, table_name, match_field_name):
        return self.get_match_field(table_name, name=match_field_name).id

    def get_match_field_name(self, table_name, match_field_id):
        return self.get_match_field(table_name, id=match_field_id).name

    def get_match_field_width(self, table_name, match_field_name):
        return self.get_match_field(table_name, name=match_field_name).bitwidth

    def get_match_field_pb(self, table_name, match_field_name, value):
        p4info_match = self.get_match_field(table_name, match_field_name)
        bitwidth = p4info_match.bitwidth
        p4runtime_match = p4runtime_pb2.FieldMatch()
        p4runtime_match.field_id = p4info_match.id
        match_type = p4info_match.match_type
        if match_type == p4info_pb2.MatchField.EXACT:
            exact = p4runtime_match.exact
            exact.value = encode(value, bitwidth)
        elif match_type == p4info_pb2.MatchField.LPM:
            lpm = p4runtime_match.lpm
            lpm.value = encode(value[0], bitwidth)
            lpm.prefix_len = value[1]
        elif match_type == p4info_pb2.MatchField.TERNARY:
            lpm = p4runtime_match.ternary
            lpm.value = encode(value[0], bitwidth)
            lpm.mask = encode(value[1], bitwidth)
        elif match_type == p4info_pb2.MatchField.RANGE:
            lpm = p4runtime_match.range
            lpm.low = encode(value[0], bitwidth)
            lpm.high = encode(value[1], bitwidth)
        else:
            raise Exception("Unsupported match type with type %r" % match_type)
        return p4runtime_match

    def get_match_field_value(self, match_field):
        match_type = match_field.WhichOneof("field_match_type")
        if match_type == 'valid':
            return match_field.valid.value
        elif match_type == 'exact':
            return match_field.exact.value
        elif match_type == 'lpm':
            return (match_field.lpm.value, match_field.lpm.prefix_len)
        elif match_type == 'ternary':
            return (match_field.ternary.value, match_field.ternary.mask)
        elif match_type == 'range':
            return (match_field.range.low, match_field.range.high)
        else:
            raise Exception("Unsupported match type with type %r" % match_type)

    def get_action_params(self, action_name):
        for a in self.p4info.actions:
            pre = a.preamble
            if pre.name == action_name:
                return a.params

    def get_action_param(self, action_name, name=None, id=None):
        for a in self.p4info.actions:
            pre = a.preamble
            if pre.name == action_name:
                for p in a.params:
                    if name is not None:
                        if p.name == name:
                            return p
                    elif id is not None:
                        if p.id == id:
                            return p
        raise AttributeError("action %r has no param %r, (has: %r)" % (action_name, name if name is not None else id, a.params))

    def get_action_param_id(self, action_name, param_name):
        return self.get_action_param(action_name, name=param_name).id

    def get_action_param_name(self, action_name, param_id):
        return self.get_action_param(action_name, id=param_id).name

    def get_action_param_pb(self, action_name, param_name, value):
        p4info_param = self.get_action_param(action_name, param_name)
        p4runtime_param = p4runtime_pb2.Action.Param()
        p4runtime_param.param_id = p4info_param.id
        p4runtime_param.value = encode(value, p4info_param.bitwidth)
        return p4runtime_param

    def buildTableEntry(self,
                        table_name,
                        match_fields=None,
                        default_action=False,
                        action_name=None,
                        action_params=None,
                        priority=None):
        table_entry = p4runtime_pb2.TableEntry()
        table_entry.table_id = self.get_tables_id(table_name)

        if priority is not None:
            table_entry.priority = priority

        if match_fields:
            table_entry.match.extend([
                self.get_match_field_pb(table_name, match_field_name, value)
                for match_field_name, value in match_fields.items()
            ])

        if default_action:
            table_entry.is_default_action = True

        if action_name:
            action = table_entry.action.action
            action.action_id = self.get_actions_id(action_name)
            if action_params:
                action.params.extend([
                    self.get_action_param_pb(action_name, field_name, value)
                    for field_name, value in action_params.items()
                ])
        return table_entry

class P4RuntimeClient:

    def __init__(self, device_id, grpc_addr='localhost:50051', election_id=(1, 0)):
        self.device_id = device_id
        self.election_id = election_id

        try:
            self.channel = grpc.insecure_channel(grpc_addr)
        except Exception as e:
            raise e
        self.stub = p4runtime_pb2_grpc.P4RuntimeStub(self.channel)
        self.set_up_stream()

    def set_up_stream(self):
        self.stream_out_q = queue.Queue()
        self.stream_in_q = queue.Queue()

        def stream_req_iterator():
            while True:
                p = self.stream_out_q.get()
                if p is None:
                    break
                yield p

        def stream_recv_wrapper(stream):
            @parse_p4runtime_error
            def stream_recv():
                for p in stream:
                    self.stream_in_q.put(p)
            try:
                stream_recv()
            except P4RuntimeException as e:
                logging.critical("StreamChannel error, closing stream")
                logging.critical(e)
                self.stream_in_q.put(None)

        self.stream = self.stub.StreamChannel(stream_req_iterator())
        self.stream_recv_thread = threading.Thread(
            target=stream_recv_wrapper, args=(self.stream,))
        self.stream_recv_thread.start()

        self.handshake()

    def handshake(self):
        req = p4runtime_pb2.StreamMessageRequest()
        arbitration = req.arbitration
        arbitration.device_id = self.device_id
        election_id = arbitration.election_id
        election_id.high = self.election_id[0]
        election_id.low = self.election_id[1]
        self.stream_out_q.put(req)

        rep = self.get_stream_packet("arbitration", timeout=2)
        if rep is None:
            logging.critical("Failed to establish session with server")
            sys.exit(1)
        is_master = (rep.arbitration.status.code == code_pb2.OK)
        logging.debug("Session established, client is '{}'".format(
            'master' if is_master else 'slave'))
        if not is_master:
            print("You are not master, you only have read access to the server")

    def get_stream_packet(self, type_, timeout=1):
        start = time.time()
        try:
            while True:
                remaining = timeout - (time.time() - start)
                if remaining < 0:
                    break
                msg = self.stream_in_q.get(timeout=remaining)
                if msg is None:
                    return None
                if not msg.HasField(type_):
                    continue
                return msg
        except queue.Empty:  # timeout expired
            pass
        return None

    @parse_p4runtime_error
    def get_p4info(self):
        req = p4runtime_pb2.GetForwardingPipelineConfigRequest()
        req.device_id = self.device_id
        req.response_type = p4runtime_pb2.GetForwardingPipelineConfigRequest.P4INFO_AND_COOKIE
        rep = self.stub.GetForwardingPipelineConfig(req)
        return rep.config.p4info

    @parse_p4runtime_error
    def set_fwd_pipe_config(self, p4info_path, bin_path):
        req = p4runtime_pb2.SetForwardingPipelineConfigRequest()
        req.device_id = self.device_id
        election_id = req.election_id
        election_id.high = self.election_id[0]
        election_id.low = self.election_id[1]
        req.action = p4runtime_pb2.SetForwardingPipelineConfigRequest.VERIFY_AND_COMMIT
        with open(p4info_path, 'r') as f1:
            with open(bin_path, 'rb') as f2:
                try:
                    google.protobuf.text_format.Merge(f1.read(), req.config.p4info)
                except google.protobuf.text_format.ParseError:
                    logging.error("Error when parsing P4Info")
                    raise
                req.config.p4_device_config = f2.read()
        return self.stub.SetForwardingPipelineConfig(req)

    def tear_down(self):
        if self.stream_out_q:
            self.stream_out_q.put(None)
            self.stream_recv_thread.join()
        self.channel.close()
        del self.channel  # avoid a race condition if channel deleted when process terminates

    @parse_p4runtime_write_error
    def write(self, req):
        req.device_id = self.device_id
        election_id = req.election_id
        election_id.high = self.election_id[0]
        election_id.low = self.election_id[1]
        return self.stub.Write(req)

    @parse_p4runtime_write_error
    def write_update(self, update):
        req = p4runtime_pb2.WriteRequest()
        req.device_id = self.device_id
        election_id = req.election_id
        election_id.high = self.election_id[0]
        election_id.low = self.election_id[1]
        req.updates.extend([update])
        return self.stub.Write(req)


def resolve_device_id_by_bridge_name(bridge_name):
    ovs = ovspy.client.OvsClient(5000)

    if not ovs.find_bridge(bridge_name):
        raise Exception("bridge '{}' doesn't exist".format(bridge_name))

    for br in ovs.get_bridge_raw():
        if br['name'] == bridge_name:
            other_configs = br['other_config'][1][0]
            for i, cfg in enumerate(other_configs):
                if cfg == 'device_id':
                    return int(other_configs[i+1])
    # This function should not reach this line
    raise Exception("bridge '{}' does not have 'device_id' configured".format(bridge_name))

def with_client(f):
    @wraps(f)
    def handle(*args, **kwargs):
        client = None
        try:
            client = P4RuntimeClient(device_id=resolve_device_id_by_bridge_name(args[0]))
            f(client, *args, **kwargs)
        except Exception as e:
            raise e
        finally:
            if client:
                client.tear_down()
    return handle

@with_client
def p4ctl_set_pipe(client, bridge):
    if len(sys.argv) < 5:
        print("ovs-p4ctl: 'set-pipe' command requires at least 3 arguments")
        return

    device_config = sys.argv[3]
    p4info = sys.argv[4]

    client.set_fwd_pipe_config(p4info, device_config)

@with_client
def p4ctl_get_pipe(client, bridge):
    p4info = client.get_p4info()
    if p4info:
        print("P4Info of bridge {}:".format(bridge))
        print(p4info)

def parse_flow(flow):
    match_keys = dict() # dict of "key:value" pairs
    tmp = flow.split(",action=")
    mk = tmp[0]
    act = tmp[1]

    mk_fields = mk.split(",")
    for mk_field in mk_fields:
        m = mk_field.split("=")
        if "/" in m[1]:
            # We have LPM key
            raise NotImplementedError("LPM not supported")
        else:
            match_keys[m[0]] = m[1]
    act_fields = act.split('(')
    action_name = act_fields[0]
    params = act_fields[1].split(')')[0]
    act_data = params.split(',')
    act_data = [ int(a) for a in act_data]
    return match_keys, action_name, act_data

@with_client
def p4ctl_add_entry(client, bridge, tbl_name, flow):
    """
    add-entry SWITCH TABLE MATCH_KEY ACTION ACTION_DATA
    Example:
        ovs-p4ctl add-entry br0 filter_tbl headers.ipv4.dstAddr=10.10.10.10,action=push_mpls(10)
    """
    if len(sys.argv) < 5:
        print("ovs-p4ctl: 'add-entry' command requires at least 3 arguments")
        return

    match_keys, action, action_data = parse_flow(flow)

    p4info = client.get_p4info()

    if not p4info:
        raise Exception("cannot retrieve P4Info from device {}".format(bridge))

    helper = P4InfoHelper(p4info)

    te = helper.buildTableEntry(
        table_name=tbl_name,
        match_fields=match_keys,
        action_name=action,
        action_params={
            a.name: action_data[idx] for idx, a in enumerate(helper.get_action_params(action))
        }
    )

    update = p4runtime_pb2.Update()
    update.type = p4runtime_pb2.Update.INSERT
    update.entity.table_entry.CopyFrom(te)

    client.write_update(update)


@with_client
def p4ctl_del_entry(client, bridge):
    raise NotImplementedError()

all_commands = {
    "set-pipe": p4ctl_set_pipe,
    "get-pipe": p4ctl_get_pipe,
    "add-entry": p4ctl_add_entry,
    "del-entry": p4ctl_del_entry,
}

def main():
    if len(sys.argv) < 2:
       print("ovs-p4ctl: missing command name; use --help for help")
       sys.exit(1)
    parser = argparse.ArgumentParser(usage=USAGE)
    parser.add_argument('command', help='Subcommand to run')

    args = parser.parse_args(sys.argv[1:2])
    if not args.command in all_commands.keys():
        usage()

    try:
        # use dispatch pattern to invoke method with same name
        all_commands[args.command](*sys.argv[2:])
    except Exception as e:
        print("Error:", str(e))
        sys.exit(1)

if __name__ == '__main__':
    main()