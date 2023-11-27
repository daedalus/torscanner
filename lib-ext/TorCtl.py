#!/usr/bin/python
# TorCtl.py -- Python module to interface with Tor Control interface.
# Copyright 2005 Nick Mathewson
# Copyright 2007 Mike Perry. See LICENSE file.

"""
Library to control Tor processes.

This library handles sending commands, parsing responses, and delivering
events to and from the control port. The basic usage is to create a
socket, wrap that in a TorCtl.Connection, and then add an EventHandler
to that connection. A simple example with a DebugEventHandler (that just
echoes the events back to stdout) is present in run_example().

Note that the TorCtl.Connection is fully compatible with the more
advanced EventHandlers in TorCtl.PathSupport (and of course any other
custom event handlers that you may extend off of those).

This package also contains a helper class for representing Routers, and
classes and constants for each event.

"""

__all__ = ["EVENT_TYPE", "TorCtlError", "TorCtlClosed", "ProtocolError",
           "ErrorReply", "NetworkStatus", "ExitPolicyLine", "Router",
           "RouterVersion", "Connection", "parse_ns_body",
           "EventHandler", "DebugEventHandler", "NetworkStatusEvent",
           "NewDescEvent", "CircuitEvent", "StreamEvent", "ORConnEvent",
           "StreamBwEvent", "LogEvent", "AddrMapEvent", "BWEvent",
           "UnknownEvent" ]

import os
import re
import struct
import sys
import threading
import Queue
import datetime
import traceback
import socket
import binascii
import types
import time
from TorUtil import *

# Types of "EVENT" message.
EVENT_TYPE = Enum2(
          CIRC="CIRC",
          STREAM="STREAM",
          ORCONN="ORCONN",
          STREAM_BW="STREAM_BW",
          BW="BW",
          NS="NS",
          NEWDESC="NEWDESC",
          ADDRMAP="ADDRMAP",
          DEBUG="DEBUG",
          INFO="INFO",
          NOTICE="NOTICE",
          WARN="WARN",
          ERR="ERR")

class TorCtlError(Exception):
  "Generic error raised by TorControl code."
  pass

class TorCtlClosed(TorCtlError):
  "Raised when the controller connection is closed by Tor (not by us.)"
  pass

class ProtocolError(TorCtlError):
  "Raised on violations in Tor controller protocol"
  pass

class ErrorReply(TorCtlError):
  "Raised when Tor controller returns an error"
  pass

class NetworkStatus:
  "Filled in during NS events"
  def __init__(self, nickname, idhash, orhash, updated, ip, orport, dirport, flags):
    self.nickname = nickname
    self.idhash = idhash
    self.orhash = orhash
    self.ip = ip
    self.orport = int(orport)
    self.dirport = int(dirport)
    self.flags = flags
    self.idhex = f"{self.idhash}=".decode("base64").encode("hex").upper()
    m = re.search(r"(\d+)-(\d+)-(\d+) (\d+):(\d+):(\d+)", updated)
    self.updated = datetime.datetime(*map(int, m.groups()))

class NetworkStatusEvent:
  def __init__(self, event_name, nslist):
    self.event_name = event_name
    self.arrived_at = 0
    self.nslist = nslist # List of NetworkStatus objects

class NewDescEvent:
  def __init__(self, event_name, idlist):
    self.event_name = event_name
    self.arrived_at = 0
    self.idlist = idlist

class CircuitEvent:
  def __init__(self, event_name, circ_id, status, path, reason,
         remote_reason):
    self.event_name = event_name
    self.arrived_at = 0
    self.circ_id = circ_id
    self.status = status
    self.path = path
    self.reason = reason
    self.remote_reason = remote_reason

class StreamEvent:
  def __init__(self, event_name, strm_id, status, circ_id, target_host,
         target_port, reason, remote_reason, source, source_addr):
    self.event_name = event_name
    self.arrived_at = 0
    self.strm_id = strm_id
    self.status = status
    self.circ_id = circ_id
    self.target_host = target_host
    self.target_port = int(target_port)
    self.reason = reason
    self.remote_reason = remote_reason
    self.source = source
    self.source_addr = source_addr

class ORConnEvent:
  def __init__(self, event_name, status, endpoint, age, read_bytes,
         wrote_bytes, reason, ncircs):
    self.event_name = event_name
    self.arrived_at = 0
    self.status = status
    self.endpoint = endpoint
    self.age = age
    self.read_bytes = read_bytes
    self.wrote_bytes = wrote_bytes
    self.reason = reason
    self.ncircs = ncircs

class StreamBwEvent:
  def __init__(self, event_name, strm_id, read, written):
    self.event_name = event_name
    self.strm_id = int(strm_id)
    self.bytes_read = int(read)
    self.bytes_written = int(written)

class LogEvent:
  def __init__(self, level, msg):
    self.event_name = self.level = level
    self.msg = msg

class AddrMapEvent:
  def __init__(self, event_name, from_addr, to_addr, when):
    self.event_name = event_name
    self.from_addr = from_addr
    self.to_addr = to_addr
    self.when = when

class BWEvent:
  def __init__(self, event_name, read, written):
    self.event_name = event_name
    self.read = read
    self.written = written

class UnknownEvent:
  def __init__(self, event_name, event_string):
    self.event_name = event_name
    self.event_string = event_string

class ExitPolicyLine:
  """ Class to represent a line in a Router's exit policy in a way 
      that can be easily checked. """
  def __init__(self, match, ip_mask, port_low, port_high):
    self.match = match
    if ip_mask == "*":
      self.ip = 0
      self.netmask = 0
    else:
      if "/" not in ip_mask:
        self.netmask = 0xFFFFFFFF
        ip = ip_mask
      else:
        ip, mask = ip_mask.split("/")
        if re.match(r"\d+.\d+.\d+.\d+", mask):
          self.netmask=struct.unpack(">I", socket.inet_aton(mask))[0]
        else:
          self.netmask = ~(2**(32 - int(mask)) - 1)
      self.ip = struct.unpack(">I", socket.inet_aton(ip))[0]
    self.ip &= self.netmask
    if port_low == "*":
      self.port_low,self.port_high = (0,65535)
    else:
      if not port_high:
        port_high = port_low
      self.port_low = int(port_low)
      self.port_high = int(port_high)
  
  def check(self, ip, port):
    """Check to see if an ip and port is matched by this line. 
     Returns true if the line is an Accept, and False if it is a Reject. """
    ip = struct.unpack(">I", socket.inet_aton(ip))[0]
    if (ip & self.netmask) == self.ip:
      if self.port_low <= port <= self.port_high:
        return self.match
    return -1

class RouterVersion:
  """ Represents a Router's version. Overloads all comparison operators
      to check for newer, older, or equivalent versions. """
  def __init__(self, version):
    if version:
      v = re.search("^(\d+).(\d+).(\d+).(\d+)", version).groups()
      self.version = int(v[0])*0x1000000 + int(v[1])*0x10000 + int(v[2])*0x100 + int(v[3])
      self.ver_string = version
    else: 
      self.version = version
      self.ver_string = "unknown"

  def __lt__(self, other): return self.version < other.version
  def __gt__(self, other): return self.version > other.version
  def __ge__(self, other): return self.version >= other.version
  def __le__(self, other): return self.version <= other.version
  def __eq__(self, other): return self.version == other.version
  def __ne__(self, other): return self.version != other.version
  def __str__(self): return self.ver_string

class Router:
  """ 
  Class to represent a router from a descriptor. Can either be
  created from the parsed fields, or can be built from a
  descriptor+NetworkStatus 
  """     
  def __init__(self, idhex, name, bw, down, exitpolicy, flags, ip, version, os, uptime):
    self.idhex = idhex
    self.nickname = name
    self.bw = bw
    self.exitpolicy = exitpolicy
    self.flags = flags
    self.down = down
    self.ip = struct.unpack(">I", socket.inet_aton(ip))[0]
    self.version = RouterVersion(version)
    self.os = os
    self.list_rank = 0 # position in a sorted list of routers.
    self.uptime = uptime

  def build_from_desc(self, ns):
    """
    Static method of Router that parses a descriptor string into this class.
    'desc' is a full descriptor as a string. 
    'ns' is a TorCtl.NetworkStatus instance for this router (needed for
    the flags, the nickname, and the idhex string). 
    Returns a Router instance.
    """
    # XXX: Compile these regular expressions? This is an expensive process
    # Use http://docs.python.org/lib/profile.html to verify this is 
    # the part of startup that is slow
    exitpolicy = []
    dead = "Running" not in ns.flags
    bw_observed = 0
    version = None
    os = None
    uptime = 0
    ip = 0
    router = "[none]"

    for line in self:
      rt = re.search(r"^router (\S+) (\S+)", line)
      fp = re.search(r"^opt fingerprint (.+).*on (\S+)", line)
      pl = re.search(r"^platform Tor (\S+).*on (\S+)", line)
      ac = re.search(r"^accept (\S+):([^-]+)(?:-(\d+))?", line)
      rj = re.search(r"^reject (\S+):([^-]+)(?:-(\d+))?", line)
      bw = re.search(r"^bandwidth \d+ \d+ (\d+)", line)
      up = re.search(r"^uptime (\d+)", line)
      if re.search(r"^opt hibernating 1", line):
        #dead = 1 # XXX: Technically this may be stale..
        if ("Running" in ns.flags):
          plog("INFO", f"Hibernating router {ns.nickname} is running..")
      if ac:
        exitpolicy.append(ExitPolicyLine(True, *ac.groups()))
      elif rj:
        exitpolicy.append(ExitPolicyLine(False, *rj.groups()))
      elif bw:
        bw_observed = int(bw.group(1))
      elif pl:
        version, os = pl.groups()
      elif up:
        uptime = int(up.group(1))
      elif rt:
        router,ip = rt.groups()
    if router != ns.nickname:
      plog("NOTICE", f"Got different names {ns.nickname} vs {router} for {ns.idhex}")
    if not bw_observed and not dead and ("Valid" in ns.flags):
      plog("INFO", f"No bandwidth for live router {ns.nickname}")
    if not version or not os:
      plog("INFO", f"No version and/or OS for router {ns.nickname}")
    return Router(ns.idhex, ns.nickname, bw_observed, dead, exitpolicy,
        ns.flags, ip, version, os, uptime)
  build_from_desc = Callable(build_from_desc)

  def update_to(self, new):
    """ Somewhat hackish method to update this router to be a copy of
    'new' """
    if self.idhex != new.idhex:
      plog("ERROR", f"Update of router {self.nickname}changes idhex!")
    self.idhex = new.idhex
    self.nickname = new.nickname
    self.bw = new.bw
    self.exitpolicy = new.exitpolicy
    self.flags = new.flags
    self.ip = new.ip
    self.version = new.version
    self.os = new.os
    self.uptime = new.uptime

  def will_exit_to(self, ip, port):
    """ Check the entire exitpolicy to see if the router will allow
        connections to 'ip':'port' """
    for line in self.exitpolicy:
      ret = line.check(ip, port)
      if ret != -1:
        return ret
    plog("WARN", f"No matching exit line for {self.nickname}")
    return False
   
class Connection:
  """A Connection represents a connection to the Tor process via the 
     control port."""
  def __init__(self, sock):
    """Create a Connection to communicate with the Tor process over the
       socket 'sock'.
    """
    self._handler = None
    self._handleFn = None
    self._sendLock = threading.RLock()
    self._queue = Queue.Queue()
    self._thread = None
    self._closedEx = None
    self._closed = 0
    self._closeHandler = None
    self._eventThread = None
    self._eventQueue = Queue.Queue()
    self._s = BufSock(sock)
    self._debugFile = None

  def set_close_handler(self, handler):
    """Call 'handler' when the Tor process has closed its connection or
       given us an exception.  If we close normally, no arguments are
       provided; otherwise, it will be called with an exception as its
       argument.
    """
    self._closeHandler = handler

  def close(self):
    """Shut down this controller connection"""
    self._sendLock.acquire()
    try:
      self._queue.put("CLOSE")
      self._eventQueue.put((time.time(), "CLOSE"))
      self._s.close()
      self._s = None
      self._closed = 1
    finally:
      self._sendLock.release()

  def launch_thread(self, daemon=1):
    """Launch a background thread to handle messages from the Tor process."""
    assert self._thread is None
    t = threading.Thread(target=self._loop)
    if daemon:
      t.setDaemon(daemon)
    t.start()
    self._thread = t
    t = threading.Thread(target=self._eventLoop)
    if daemon:
      t.setDaemon(daemon)
    t.start()
    self._eventThread = t
    return self._thread

  def _loop(self):
    """Main subthread loop: Read commands from Tor, and handle them either
       as events or as responses to other commands.
    """
    while 1:
      try:
        isEvent, reply = self._read_reply()
      except:
        self._err(sys.exc_info())
        return

      if isEvent:
        if self._handler is not None:
          self._eventQueue.put((time.time(), reply))
      else:
        cb = self._queue.get() # atomic..
        cb(reply)

  def _err(self, (tp, ex, tb), fromEventLoop=0):
    """DOCDOC"""
    # silent death is bad :(
    traceback.print_exception(tp, ex, tb)
    if self._s:
      try:
        self.close()
      except:
        pass
    self._sendLock.acquire()
    try:
      self._closedEx = ex
      self._closed = 1
    finally:
      self._sendLock.release()
    while 1:
      try:
        cb = self._queue.get(timeout=0)
        if cb != "CLOSE":
          cb("EXCEPTION")
      except Queue.Empty:
        break
    if self._closeHandler is not None:
      self._closeHandler(ex)
    return

  def _eventLoop(self):
    """DOCDOC"""
    while 1:
      (timestamp, reply) = self._eventQueue.get()
      if reply[0][0] == "650" and reply[0][1] == "OK":
        plog("DEBUG", "Ignoring incompatible syntactic sugar: 650 OK")
        continue
      if reply == "CLOSE":
        return
      try:
        self._handleFn(timestamp, reply)
      except:
        for code, msg, data in reply:
          plog("WARN", f"No event for: {str(code)} {str(msg)}")
        self._err(sys.exc_info(), 1)
        return

  def _sendImpl(self, sendFn, msg):
    """DOCDOC"""
    if self._thread is None:
      self.launch_thread(1)
    # This condition will get notified when we've got a result...
    condition = threading.Condition()
    # Here's where the result goes...
    result = []

    if self._closedEx is not None:
      raise self._closedEx
    elif self._closed:
      raise TorCtlClosed()

    def cb(reply,condition=condition,result=result):
      condition.acquire()
      try:
        result.append(reply)
        condition.notify()
      finally:
        condition.release()

    # Sends a message to Tor...
    self._sendLock.acquire() # ensure queue+sendmsg is atomic
    try:
      self._queue.put(cb)
      sendFn(msg) # _doSend(msg)
    finally:
      self._sendLock.release()

    # Now wait till the answer is in...
    condition.acquire()
    try:
      while not result:
        condition.wait()
    finally:
      condition.release()

    # ...And handle the answer appropriately.
    assert len(result) == 1
    reply = result[0]
    if reply == "EXCEPTION":
      raise self._closedEx

    return reply


  def debug(self, f):
    """DOCDOC"""
    self._debugFile = f

  def set_event_handler(self, handler):
    """Cause future events from the Tor process to be sent to 'handler'.
    """
    self._handler = handler
    self._handleFn = handler._handle1

  def _read_reply(self):
    lines = []
    while 1:
      line = self._s.readline().strip()
      if self._debugFile:
        self._debugFile.write("  %s\n" % line)
      if len(line)<4:
        raise ProtocolError("Badly formatted reply line: Too short")
      code = line[:3]
      tp = line[3]
      s = line[4:]
      if tp == "-":
        lines.append((code, s, None))
      elif tp == " ":
        lines.append((code, s, None))
        isEvent = (lines and lines[0][0][0] == '6')
        return isEvent, lines
      elif tp != "+":
        raise ProtocolError("Badly formatted reply line: unknown type %r"%tp)
      else:
        more = []
        while 1:
          line = self._s.readline()
          if self._debugFile:
            self._debugFile.write(f"+++ {line}")
          if line in (".\r\n", ".\n", "650 OK\n", "650 OK\r\n"): 
            break
          more.append(line)
        lines.append((code, s, unescape_dots("".join(more))))
        if isEvent := (lines and lines[0][0][0] == '6'):
          return (isEvent, lines)

    # Notreached
    raise TorCtlError()

  def _doSend(self, msg):
    if self._debugFile:
      amsg = msg
      lines = amsg.split("\n")
      if len(lines) > 2:
        amsg = "\n".join(lines[:2]) + "\n"
      self._debugFile.write(f">>> {amsg}")
    self._s.write(msg)

  def sendAndRecv(self, msg="", expectedTypes=("250", "251")):
    """Helper: Send a command 'msg' to Tor, and wait for a command
       in response.  If the response type is in expectedTypes,
       return a list of (tp,body,extra) tuples.  If it is an
       error, raise ErrorReply.  Otherwise, raise ProtocolError.
    """
    if type(msg) == types.ListType:
      msg = "".join(msg)
    assert msg.endswith("\r\n")

    lines = self._sendImpl(self._doSend, msg)
    # print lines
    for tp, msg, _ in lines:
      if tp[0] in '45':
        raise ErrorReply(f"{tp} {msg}")
      if tp not in expectedTypes:
        raise ProtocolError("Unexpectd message type %r"%tp)

    return lines

  def authenticate(self, secret=""):
    """Send an authenticating secret to Tor.  You'll need to call this
       method before Tor can start.
    """
    #hexstr = binascii.b2a_hex(secret)
    self.sendAndRecv("AUTHENTICATE \"%s\"\r\n"%secret)

  def get_option(self, name):
    """Get the value of the configuration option named 'name'.  To
       retrieve multiple values, pass a list for 'name' instead of
       a string.  Returns a list of (key,value) pairs.
       Refer to section 3.3 of control-spec.txt for a list of valid names.
    """
    if not isinstance(name, str):
      name = " ".join(name)
    lines = self.sendAndRecv("GETCONF %s\r\n" % name)

    r = []
    for _,line,_ in lines:
      try:
        key, val = line.split("=", 1)
        r.append((key,val))
      except ValueError:
        r.append((line, None))

    return r

  def set_option(self, key, value):
    """Set the value of the configuration option 'key' to the value 'value'.
    """
    self.set_options([(key, value)])

  def set_options(self, kvlist):
    """Given a list of (key,value) pairs, set them as configuration
       options.
    """
    if not kvlist:
      return
    msg = " ".join([f"{k}={quote(v)}" for k,v in kvlist])
    self.sendAndRecv("SETCONF %s\r\n"%msg)

  def reset_options(self, keylist):
    """Reset the options listed in 'keylist' to their default values.

       Tor started implementing this command in version 0.1.1.7-alpha;
       previous versions wanted you to set configuration keys to "".
       That no longer works.
    """
    self.sendAndRecv("RESETCONF %s\r\n"%(" ".join(keylist)))

  def get_network_status(self, who="all"):
    """Get the entire network status list. Returns a list of
       TorCtl.NetworkStatus instances."""
    return parse_ns_body(self.sendAndRecv(f"GETINFO ns/{who}" + "\r\n")[0][2])

  def get_router(self, ns):
    """Fill in a Router class corresponding to a given NS class"""
    desc = self.sendAndRecv(f"GETINFO desc/id/{ns.idhex}" +
                            "\r\n")[0][2].split("\n")
    return Router.build_from_desc(desc, ns)


  def read_routers(self, nslist):
    """ Given a list a NetworkStatuses in 'nslist', this function will 
        return a list of new Router instances.
    """
    bad_key = 0
    new = []
    for ns in nslist:
      try:
        r = self.get_router(ns)
        new.append(r)
      except ErrorReply:
        bad_key += 1
        if "Running" in ns.flags:
          plog("NOTICE", f"Running router {ns.nickname}={ns.idhex} has no descriptor")
      except:
        traceback.print_exception(*sys.exc_info())
        continue

    return new

  def get_info(self, name):
    """Return the value of the internal information field named 'name'.
       Refer to section 3.9 of control-spec.txt for a list of valid names.
       DOCDOC
    """
    if not isinstance(name, str):
      name = " ".join(name)
    lines = self.sendAndRecv("GETINFO %s\r\n"%name)
    d = {}
    for _,msg,more in lines:
      if msg == "OK":
        break
      try:
        k,rest = msg.split("=",1)
      except ValueError:
        raise ProtocolError("Bad info line %r",msg)
      d[k] = more if more else rest
    return d

  def set_events(self, events, extended=False):
    """Change the list of events that the event handler is interested
       in to those in 'events', which is a list of event names.
       Recognized event names are listed in section 3.3 of the control-spec
    """
    if extended:
      plog ("DEBUG", "SETEVENTS EXTENDED %s\r\n" % " ".join(events))
      self.sendAndRecv("SETEVENTS EXTENDED %s\r\n" % " ".join(events))
    else:
      self.sendAndRecv("SETEVENTS %s\r\n" % " ".join(events))

  def save_conf(self):
    """Flush all configuration changes to disk.
    """
    self.sendAndRecv("SAVECONF\r\n")

  def send_signal(self, sig):
    """Send the signal 'sig' to the Tor process; The allowed values for
       'sig' are listed in section 3.6 of control-spec.
    """
    sig = { 0x01 : "HUP",
        0x02 : "INT",
        0x0A : "USR1",
        0x0C : "USR2",
        0x0F : "TERM" }.get(sig,sig)
    self.sendAndRecv("SIGNAL %s\r\n"%sig)

  def resolve(self, host):
    """ Launch a remote hostname lookup request:
        'host' may be a hostname or IPv4 address
    """
    # TODO: handle "mode=reverse"
    self.sendAndRecv("RESOLVE %s\r\n"%host)

  def map_address(self, kvList):
    """ Sends the MAPADDRESS command for each of the tuples in kvList """
    if not kvList:
      return
    m = " ".join([ "%s=%s" for k,v in kvList])
    lines = self.sendAndRecv("MAPADDRESS %s\r\n"%m)
    r = []
    for _,line,_ in lines:
      try:
        key, val = line.split("=", 1)
      except ValueError:
        raise ProtocolError("Bad address line %r",v)
      r.append((key,val))
    return r

  def extend_circuit(self, circid, hops):
    """Tell Tor to extend the circuit identified by 'circid' through the
       servers named in the list 'hops'.
    """
    if circid is None:
      circid = "0"
    plog("DEBUG", "Extending circuit")
    lines = self.sendAndRecv("EXTENDCIRCUIT %d %s\r\n"
                  %(circid, ",".join(hops)))
    tp,msg,_ = lines[0]
    m = re.match(r'EXTENDED (\S*)', msg)
    if not m:
      raise ProtocolError("Bad extended line %r",msg)
    plog("DEBUG", "Circuit extended")
    return int(m.group(1))

  def redirect_stream(self, streamid, newaddr, newport=""):
    """DOCDOC"""
    if newport:
      self.sendAndRecv("REDIRECTSTREAM %d %s %s\r\n"%(streamid, newaddr, newport))
    else:
      self.sendAndRecv("REDIRECTSTREAM %d %s\r\n"%(streamid, newaddr))

  def attach_stream(self, streamid, circid, hop=None):
    """Attach a stream to a circuit, specify both by IDs. If hop is given, 
       try to use the specified hop in the circuit as the exit node for 
       this stream.
    """
    if hop:
      self.sendAndRecv("ATTACHSTREAM %d %d HOP=%d\r\n"%(streamid, circid, hop))
      plog(
          "DEBUG",
          f"Attaching stream: {str(streamid)} to hop {str(hop)} of circuit {str(circid)}",
      )
    else:
      self.sendAndRecv("ATTACHSTREAM %d %d\r\n"%(streamid, circid))
      plog("DEBUG", f"Attaching stream: {str(streamid)} to circuit {str(circid)}")

  def close_stream(self, streamid, reason=0, flags=()):
    """DOCDOC"""
    self.sendAndRecv("CLOSESTREAM %d %s %s\r\n"
              %(streamid, reason, "".join(flags)))

  def close_circuit(self, circid, reason=0, flags=()):
    """DOCDOC"""
    self.sendAndRecv("CLOSECIRCUIT %d %s %s\r\n"
              %(circid, reason, "".join(flags)))

  def post_descriptor(self, desc):
    self.sendAndRecv("+POSTDESCRIPTOR\r\n%s"%escape_dots(desc))

def parse_ns_body(data):
  """Parse the body of an NS event or command into a list of
     NetworkStatus instances"""
  nsgroups = re.compile(r"^r ", re.M).split(data)
  nsgroups.pop(0)
  nslist = []
  for nsline in nsgroups:
    m = re.search(r"^s((?:\s\S*)+)", nsline, re.M)
    flags = m.groups()
    flags = flags[0].strip().split(" ")
    m = re.match(r"(\S+)\s(\S+)\s(\S+)\s(\S+\s\S+)\s(\S+)\s(\d+)\s(\d+)", nsline)
    nslist.append(NetworkStatus(*(m.groups() + (flags,))))
  return nslist

class EventHandler:
  """An 'EventHandler' wraps callbacks for the events Tor can return. 
     Each event argument is an instance of the corresponding event
     class."""
  def __init__(self):
    """Create a new EventHandler."""
    self._map1 = {
      "CIRC" : self.circ_status_event,
      "STREAM" : self.stream_status_event,
      "ORCONN" : self.or_conn_status_event,
      "STREAM_BW" : self.stream_bw_event,
      "BW" : self.bandwidth_event,
      "DEBUG" : self.msg_event,
      "INFO" : self.msg_event,
      "NOTICE" : self.msg_event,
      "WARN" : self.msg_event,
      "ERR" : self.msg_event,
      "NEWDESC" : self.new_desc_event,
      "ADDRMAP" : self.address_mapped_event,
      "NS" : self.ns_event
      }

  def _handle1(self, timestamp, lines):
    """Dispatcher: called from Connection when an event is received."""
    for code, msg, data in lines:
      event = self._decode1(msg, data)
      event.arrived_at = timestamp
      self.heartbeat_event(event)
      self._map1.get(event.event_name, self.unknown_event)(event)

  def _decode1(self, body, data):
    """Unpack an event message into a type/arguments-tuple tuple."""
    evtype,body = body.split(" ",1) if " " in body else (body, "")
    evtype = evtype.upper()
    if evtype == "CIRC":
      m = re.match(r"(\d+)\s+(\S+)(\s\S+)?(\s\S+)?(\s\S+)?", body)
      if not m:
        raise ProtocolError("CIRC event misformatted.")
      ident,status,path,reason,remote = m.groups()
      ident = int(ident)
      if path:
        if "REASON=" in path:
          remote = reason
          reason = path
          path=[]
        else:
          path = path.strip().split(",")
      else:
        path = []
      if reason: reason = reason[8:]
      if remote: remote = remote[15:]
      return CircuitEvent(evtype, ident, status, path, reason, remote)
    elif evtype == "STREAM":
      #plog("DEBUG", "STREAM: "+body)
      m = re.match(r"(\S+)\s+(\S+)\s+(\S+)\s+(\S+):(\d+)(\sREASON=\S+)?(\sREMOTE_REASON=\S+)?(\sSOURCE=\S+)?(\sSOURCE_ADDR=\S+)?", body)
      if not m:
        raise ProtocolError("STREAM event misformatted.")
      ident,status,circ,target_host,target_port,reason,remote,source,source_addr = m.groups()
      ident,circ = map(int, (ident,circ))
      if reason: reason = reason[8:]
      if remote: remote = remote[15:]
      if source: source = source[8:]
      if source_addr: source_addr = source_addr[13:]
      return StreamEvent(
          evtype,
          ident,
          status,
          circ,
          target_host,
          int(target_port),
          reason,
          remote,
          source,
          source_addr,
      )
    elif evtype == "ORCONN":
      m = re.match(r"(\S+)\s+(\S+)(\sAGE=\S+)?(\sREAD=\S+)?(\sWRITTEN=\S+)?(\sREASON=\S+)?(\sNCIRCS=\S+)?", body)
      if not m:
        raise ProtocolError("ORCONN event misformatted.")
      target, status, age, read, wrote, reason, ncircs = m.groups()

      #plog("DEBUG", "ORCONN: "+body)
      ncircs = int(ncircs[8:]) if ncircs else 0
      if reason: reason = reason[8:]
      age = int(age[5:]) if age else 0
      read = int(read[6:]) if read else 0
      wrote = int(wrote[9:]) if wrote else 0
      return ORConnEvent(evtype, status, target, age, read, wrote, reason,
                         ncircs)
    elif evtype == "STREAM_BW":
      m = re.match(r"(\d+)\s+(\d+)\s+(\d+)", body)
      if not m:
        raise ProtocolError("STREAM_BW event misformatted.")
      return StreamBwEvent(evtype, *m.groups())
    elif evtype == "BW":
      m = re.match(r"(\d+)\s+(\d+)", body)
      if not m:
        raise ProtocolError("BANDWIDTH event misformatted.")
      read, written = map(long, m.groups())
      return BWEvent(evtype, read, written)
    elif evtype in ("DEBUG", "INFO", "NOTICE", "WARN", "ERR"):
      return LogEvent(evtype, body)
    elif evtype == "NEWDESC":
      return NewDescEvent(evtype, body.split(" "))
    elif evtype == "ADDRMAP":
      # TODO: Also parse errors and GMTExpiry
      m = re.match(r'(\S+)\s+(\S+)\s+(\"[^"]+\"|\w+)', body)
      if not m:
        raise ProtocolError("ADDRMAP event misformatted.")
      fromaddr, toaddr, when = m.groups()
      when = (None if when.upper() == "NEVER" else time.strptime(
          when[1:-1], "%Y-%m-%d %H:%M:%S"))
      return AddrMapEvent(evtype, fromaddr, toaddr, when)
    elif evtype == "NS":
      return NetworkStatusEvent(evtype, parse_ns_body(data))
    else:
      return UnknownEvent(evtype, body)

  def heartbeat_event(self, event):
    """Called before any event is recieved. Convenience function
       for any cleanup/setup/reconfiguration you may need to do.
    """
    pass

  def unknown_event(self, event):
    """Called when we get an event type we don't recognize.  This
       is almost alwyas an error.
    """
    raise NotImplemented()

  def circ_status_event(self, event):
    """Called when a circuit status changes if listening to CIRCSTATUS
       events."""
    raise NotImplemented()

  def stream_status_event(self, event):
    """Called when a stream status changes if listening to STREAMSTATUS
       events.  """
    raise NotImplemented()

  def stream_bw_event(self, event):
    raise NotImplemented()

  def or_conn_status_event(self, event):
    """Called when an OR connection's status changes if listening to
       ORCONNSTATUS events."""
    raise NotImplemented()

  def bandwidth_event(self, event):
    """Called once a second if listening to BANDWIDTH events.
    """
    raise NotImplemented()

  def new_desc_event(self, event):
    """Called when Tor learns a new server descriptor if listenting to
       NEWDESC events.
    """
    raise NotImplemented()

  def msg_event(self, event):
    """Called when a log message of a given severity arrives if listening
       to INFO_MSG, NOTICE_MSG, WARN_MSG, or ERR_MSG events."""
    raise NotImplemented()

  def ns_event(self, event):
    raise NotImplemented()

  def address_mapped_event(self, event):
    """Called when Tor adds a mapping for an address if listening
       to ADDRESSMAPPED events.
    """
    raise NotImplemented()


class DebugEventHandler(EventHandler):
  """Trivial debug event handler: reassembles all parsed events to stdout."""
  def circ_status_event(self, circ_event): # CircuitEvent()
    output = [circ_event.event_name, str(circ_event.circ_id),
          circ_event.status]
    if circ_event.path:
      output.append(",".join(circ_event.path))
    if circ_event.reason:
      output.append("REASON=" + circ_event.reason)
    if circ_event.remote_reason:
      output.append("REMOTE_REASON=" + circ_event.remote_reason)
    print " ".join(output)

  def stream_status_event(self, strm_event):
    output = [strm_event.event_name, str(strm_event.strm_id),
          strm_event.status, str(strm_event.circ_id),
          strm_event.target_host, str(strm_event.target_port)]
    if strm_event.reason:
      output.append("REASON=" + strm_event.reason)
    if strm_event.remote_reason:
      output.append("REMOTE_REASON=" + strm_event.remote_reason)
    print " ".join(output)

  def ns_event(self, ns_event):
    for ns in ns_event.nslist:
      print " ".join((ns_event.event_name, ns.nickname, ns.idhash,
        ns.updated.isoformat(), ns.ip, str(ns.orport),
        str(ns.dirport), " ".join(ns.flags)))

  def new_desc_event(self, newdesc_event):
    print " ".join((newdesc_event.event_name, " ".join(newdesc_event.idlist)))
   
  def or_conn_status_event(self, orconn_event):
    if orconn_event.age: age = "AGE="+str(orconn_event.age)
    else: age = ""
    if orconn_event.read_bytes: read = "READ="+str(orconn_event.read_bytes)
    else: read = ""
    if orconn_event.wrote_bytes: wrote = "WRITTEN="+str(orconn_event.wrote_bytes)
    else: wrote = ""
    if orconn_event.reason: reason = "REASON="+orconn_event.reason
    else: reason = ""
    if orconn_event.ncircs: ncircs = "NCIRCS="+str(orconn_event.ncircs)
    else: ncircs = ""
    print " ".join((orconn_event.event_name, orconn_event.endpoint,
            orconn_event.status, age, read, wrote, reason, ncircs))

  def msg_event(self, log_event):
    print log_event.event_name+" "+log_event.msg
  
  def bandwidth_event(self, bw_event):
    print bw_event.event_name+" "+str(bw_event.read)+" "+str(bw_event.written)

def parseHostAndPort(h):
  """Given a string of the form 'address:port' or 'address' or
     'port' or '', return a two-tuple of (address, port)
  """
  host, port = "localhost", 9100
  if ":" in h:
    i = h.index(":")
    host = h[:i]
    try:
      port = int(h[i+1:])
    except ValueError:
      print "Bad hostname %r"%h
      sys.exit(1)
  elif h:
    try:
      port = int(h)
    except ValueError:
      host = h

  return host, port

def run_example(host,port):
  """ Example of basic TorCtl usage. See PathSupport for more advanced
      usage.
  """
  print "host is %s:%d"%(host,port)
  s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
  s.connect((host,port))
  c = Connection(s)
  c.set_event_handler(DebugEventHandler())
  th = c.launch_thread()
  c.authenticate()
  print "nick",`c.get_option("nickname")`
  print `c.get_info("version")`
  #print `c.get_info("desc/name/moria1")`
  print `c.get_info("network-status")`
  print `c.get_info("addr-mappings/all")`
  print `c.get_info("addr-mappings/config")`
  print `c.get_info("addr-mappings/cache")`
  print `c.get_info("addr-mappings/control")`

  print `c.extend_circuit(0,["moria1"])`
  try:
    print `c.extend_circuit(0,[""])`
  except ErrorReply: # wtf?
    print "got error. good."
  except:
    print "Strange error", sys.exc_info()[0]
   
  #send_signal(s,1)
  #save_conf(s)

  #set_option(s,"1")
  #set_option(s,"bandwidthburstbytes 100000")
  #set_option(s,"runasdaemon 1")
  #set_events(s,[EVENT_TYPE.WARN])
#  c.set_events([EVENT_TYPE.ORCONN], True)
  c.set_events([EVENT_TYPE.STREAM, EVENT_TYPE.CIRC,
          EVENT_TYPE.NS, EVENT_TYPE.NEWDESC,
          EVENT_TYPE.ORCONN, EVENT_TYPE.BW], True)

  th.join()
  return

if __name__ == '__main__':
  if len(sys.argv) > 2:
    print "Syntax: TorControl.py torhost:torport"
    sys.exit(0)
  else:
    sys.argv.append("localhost:9051")
  sh,sp = parseHostAndPort(sys.argv[1])
  run_example(sh,sp)

