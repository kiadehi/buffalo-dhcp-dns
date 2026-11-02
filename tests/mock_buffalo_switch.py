#!/usr/bin/env python3
"""
Mock BUFFALO BS-G3024MR Telnet Server for End-to-End Testing
"""

import socket
import sys
import threading
import time


class MockBSG3024MRServer:
    def __init__(self, host="127.0.0.1", port=2327):
        self.host = host
        self.port = port
        self.server_sock = None
        self.thread = None
        self.running = False

    def start(self):
        self.server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_sock.bind((self.host, self.port))
        self.server_sock.listen(1)
        self.running = True
        self.thread = threading.Thread(target=self._run)
        self.thread.daemon = True
        self.thread.start()
        time.sleep(0.3)

    def stop(self):
        self.running = False
        if self.server_sock:
            try:
                self.server_sock.close()
            except Exception:
                pass

    def _run(self):
        while self.running:
            try:
                self.server_sock.settimeout(1.0)
                try:
                    conn, addr = self.server_sock.accept()
                except socket.timeout:
                    continue

                self._handle_client(conn)
            except Exception:
                break

    def _handle_client(self, conn: socket.socket):
        try:
            conn.sendall(b"User Access Verification\r\n\r\nusername: ")
            conn.recv(1024)
            conn.sendall(b"password: ")
            conn.recv(1024)

            # Curses Main Menu
            conn.sendall(
                b"\x1b[2J\r\n"
                b"====================================================\r\n"
                b"           BUFFALO BS-G3024MR Main Menu             \r\n"
                b"====================================================\r\n"
                b"   [1] System Configuration\r\n"
                b"   [2] Port Configuration\r\n"
                b"   [N] Command Line Interface\r\n"
                b"----------------------------------------------------\r\n"
                b"<Tab> Move, <Ctrl-L> Redraw, <Ctrl-W> Save, <Enter> Select\r\n"
                b"Enter Choice: "
            )
            choice = conn.recv(1024)

            # User Mode CLI
            conn.sendall(b"\r\nBS-G3024MR> ")
            cmd1 = conn.recv(1024)

            # Privileged Mode
            conn.sendall(b"\r\nBS-G3024MR# ")
            cmd2 = conn.recv(1024) # show ip dhcp binding

            # DHCP Lease Table Output
            output = (
                b"\r\n"
                b" IP                   MAC                      LeaseTime\r\n"
                b" -----------------------------------------------------------------\r\n"
                b" 192.168.20.102       00:11:22:33:44:01        Oct 31 08:54:24  2026\r\n"
                b" 192.168.20.103       00:11:22:33:44:02        Oct 31 08:31:42  2026\r\n"
                b" 192.168.100.100      00:11:22:33:44:03        Oct 31 08:50:11  2026\r\n"
                b"BS-G3024MR# "
            )
            conn.sendall(output)
            cmd4 = conn.recv(1024) # exit
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    server = MockBSG3024MRServer()
    server.start()
    print(f"Mock server running on {server.host}:{server.port}...")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        server.stop()

