#!/usr/bin/python
# TorCtl.py -- Python module to interface with Tor Control interface.
# Copyright 2007 Mike Perry -- See LICENSE for licensing information.
# Portions Copyright 2005 Nick Matthewson

"""
TorUtil -- Support functions for TorCtl.py and metatroller
"""

import os
import re
import sys
import socket
import binascii
import sha
import math

__all__ = ["Enum", "Enum2", "Callable", "sort_list", "quote", "escape_dots", "unescape_dots",
      "BufSock", "secret_to_key", "urandom_rng", "s2k_gen", "s2k_check", "plog", 
     "ListenSocket", "zprob"]

class Enum:
  """ Defines an ordered dense name-to-number 1-1 mapping """
  def __init__(self, start, names):
    self.nameOf = {}
    idx = start
    for name in names:
      setattr(self,name,idx)
      self.nameOf[idx] = name
      idx += 1

class Enum2:
  """ Defines an ordered sparse name-to-number 1-1 mapping """
  def __init__(self, **args):
    self.__dict__.update(args)
    self.nameOf = {}
    for k,v in args.items():
      self.nameOf[v] = k

class Callable:
    def __init__(self, anycallable):
        self.__call__ = anycallable

def sort_list(list, key):
  """ Sort a list by a specified key """
  list.sort(lambda x,y: cmp(key(x), key(y))) # Python < 2.4 hack
  return list

def quote(s):
  return re.sub(r'([\r\n\\\"])', r'\\\1', s)

def escape_dots(s, translate_nl=1):
  lines = re.split(r"\r?\n", s) if translate_nl else s.split("\r\n")
  if lines and not lines[-1]:
    del lines[-1]
  for i in xrange(len(lines)):
    if lines[i].startswith("."):
      lines[i] = f".{lines[i]}"
  lines.append(".\r\n")
  return "\r\n".join(lines)

def unescape_dots(s, translate_nl=1):
  lines = s.split("\r\n")

  for i in xrange(len(lines)):
    if lines[i].startswith("."):
      lines[i] = lines[i][1:]

  if lines and lines[-1]:
    lines.append("")

  return "\n".join(lines) if translate_nl else "\r\n".join(lines)

# XXX: Exception handling
class BufSock:
  def __init__(self, s):
    self._s = s
    self._buf = []

  def readline(self):
    if self._buf:
      idx = self._buf[0].find('\n')
      if idx >= 0:
        result = self._buf[0][:idx+1]
        self._buf[0] = self._buf[0][idx+1:]
        return result

    while 1:
      s = self._s.recv(128)
      if not s: return None
      # XXX: This really does need an exception
      #  raise ConnectionClosed()
      idx = s.find('\n')
      if idx >= 0:
        self._buf.append(s[:idx+1])
        result = "".join(self._buf)
        if rest := s[idx + 1:]:
          self._buf = [ rest ]
        else:
          del self._buf[:]
        return result
      else:
        self._buf.append(s)

  def write(self, s):
    self._s.send(s)

  def close(self):
    self._s.close()

# SocketServer.TCPServer is nuts.. 
class ListenSocket:
  def __init__(self, listen_ip, port):
    msg = None
    self.s = None
    for res in socket.getaddrinfo(listen_ip, port, socket.AF_UNSPEC,
              socket.SOCK_STREAM, 0, socket.AI_PASSIVE):
      af, socktype, proto, canonname, sa = res
      try:
        self.s = socket.socket(af, socktype, proto)
        self.s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
      except socket.error, msg:
        self.s = None
        continue
      try:
        self.s.bind(sa)
        self.s.listen(1)
      except socket.error, msg:
        self.s.close()
        self.s = None
        continue
      break
    if self.s is None:
      raise socket.error(msg)

  def accept(self):
    conn, addr = self.s.accept()
    return conn

  def close(self):
    self.s.close()


def secret_to_key(secret, s2k_specifier):
  """Used to generate a hashed password string. DOCDOC."""
  c = ord(s2k_specifier[8])
  EXPBIAS = 6
  count = (16+(c&15)) << ((c>>4) + EXPBIAS)

  d = sha.new()
  tmp = s2k_specifier[:8]+secret
  slen = len(tmp)
  while count:
    if count > slen:
      d.update(tmp)
      count -= slen
    else:
      d.update(tmp[:count])
      count = 0
  return d.digest()

def urandom_rng(n):
  """Try to read some entropy from the platform entropy source."""
  f = open('/dev/urandom', 'rb')
  try:
    return f.read(n)
  finally:
    f.close()

def s2k_gen(secret, rng=None):
  """DOCDOC"""
  if rng is None:
    rng = os.urandom if hasattr(os, "urandom") else urandom_rng
  spec = f"{rng(8)}{chr(96)}"
  return f"16:{binascii.b2a_hex(spec + secret_to_key(secret, spec))}"

def s2k_check(secret, k):
  """DOCDOC"""
  assert k[:3] == "16:"

  k =  binascii.a2b_hex(k[3:])
  return secret_to_key(secret, k[:9]) == k[9:]


## XXX: Make this a class?
loglevel = "DEBUG"
loglevels = {"DEBUG" : 0, "INFO" : 1, "NOTICE" : 2, "WARN" : 3, "ERROR" : 4}

def plog(level, msg): # XXX: Timestamps
  if(loglevels[level] >= loglevels[loglevel]):
    print level + ": " + msg
    sys.stdout.flush()

# Stolen from
# http://www.nmr.mgh.harvard.edu/Neural_Systems_Group/gary/python/stats.py
def zprob(z):
  """
Returns the area under the normal curve 'to the left of' the given z value.
Thus, 
    for z<0, zprob(z) = 1-tail probability
    for z>0, 1.0-zprob(z) = 1-tail probability
    for any z, 2.0*(1.0-zprob(abs(z))) = 2-tail probability
Adapted from z.c in Gary Perlman's |Stat.

Usage:   lzprob(z)
"""
  if z == 0.0:
    x = 0.0
  else:
    y = 0.5 * math.fabs(z)
    Z_MAX = 6.0    # maximum meaningful z-value
    if y >= (Z_MAX*0.5):
        x = 1.0
    elif (y < 1.0):
        w = y*y
        x = ((((((((0.000124818987 * w
                    -0.001075204047) * w +0.005198775019) * w
                  -0.019198292004) * w +0.059054035642) * w
                -0.151968751364) * w +0.319152932694) * w
              -0.531923007300) * w +0.797884560593) * y * 2.0
    else:
        y = y - 2.0
        x = (((((((((((((-0.000045255659 * y
                         +0.000152529290) * y -0.000019538132) * y
                       -0.000676904986) * y +0.001390604284) * y
                     -0.000794620820) * y -0.002034254874) * y
                   +0.006549791214) * y -0.010557625006) * y
                 +0.011630447319) * y -0.009279453341) * y
               +0.005353579108) * y -0.002141268741) * y
             +0.000535310849) * y +0.999936657524
  return ((x+1.0)*0.5) if z > 0.0 else ((1.0-x)*0.5)

