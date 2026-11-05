"""Рисует значок приложения: тот же кубик, что в шапке панели. Запуск: python make_icon.py (нужен Pillow)"""
from pathlib import Path

from PIL import Image, ImageDraw

SCALE = 16
TOP = ((16, 3), (29, 10), (16, 17), (3, 10))
LEFT = ((3, 10), (16, 17), (16, 30), (3, 23))
RIGHT = ((29, 10), (16, 17), (16, 30), (29, 23))
FACES = ((TOP, "#7fd277"), (LEFT, "#5fb85a"), (RIGHT, "#3e8a3a"))
SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
TARGET = Path(__file__).resolve().parent.parent / "spawnbox" / "static" / "icon.ico"


def main():
    side = 32 * SCALE
    image = Image.new("RGBA", (side, side))
    draw = ImageDraw.Draw(image)
    for points, color in FACES:
        draw.polygon([(x * SCALE, y * SCALE) for x, y in points], fill=color)
    image = image.resize((256, 256), Image.LANCZOS)
    image.save(TARGET, sizes=SIZES)


if __name__ == "__main__":
    main()
