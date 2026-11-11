"""Делает QR-код адреса страницы для телефона: python tools/make_qr.py [адрес]"""
import socket
import sys
from pathlib import Path

import qrcode
import qrcode.image.svg


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    finally:
        s.close()


url = sys.argv[1] if len(sys.argv) > 1 else "http://dnevnik.lan"   # страница живёт на роутере, имя dnevnik.lan не зависит от его адреса
out = Path(__file__).resolve().parent.parent / "QR-для-телефона.svg"
qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, box_size=14, border=3).save(str(out))
print(f"{url} -> {out}")
