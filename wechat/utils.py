import copy
import typing
import pathlib
import subprocess

import psutil
import xmltodict

BASE_DIR = pathlib.Path(__file__).resolve().parent

TOOLS = BASE_DIR / "tools"
HOOK = TOOLS / "hook.exe"


def hook(pid: int, ip: str, port: int, callback_url) -> subprocess.Popen:
    return subprocess.Popen(f"{HOOK} {pid} {ip}:{port} {callback_url}", stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)


def get_processes(process_name: str) -> typing.List[psutil.Process]:
    processes = []
    for process in psutil.process_iter():
        if process.name().lower() == process_name.lower():
            processes.append(process)
    return processes


def parse_xml(xml: str) -> dict:
    return xmltodict.parse(xml)


def parse_event(event: dict) -> dict:
    data = copy.deepcopy(event)
    for field in ["raw_msg"]:
        try:
            data["data"][field] = parse_xml(data["data"][field])
        except Exception:
            pass
    return data


def get_image_info(data: bytes) -> typing.Union[typing.Tuple[str, int], None]:
    if not data:
        raise Exception("data is empty!")

    JPEG = (0xFF, 0XD8, 0XFF)
    PNG = (0x89, 0x50, 0x4E)
    BMP = (0x42, 0x4D)
    GIF = (0x47, 0x49, 0x46)
    IMAGE_FORMAT_FEATURE = [JPEG, PNG, BMP, GIF]
    IMAGE_FORMAT = {0: "jpg", 1: "png", 2: "bmp", 3: "gif"}

    for i, FORMAT_FEATURE in enumerate(IMAGE_FORMAT_FEATURE):
        result = []
        image_feature = data[:len(FORMAT_FEATURE)]
        for j, format_feature in enumerate(FORMAT_FEATURE):
            result.append(image_feature[j] ^ format_feature)

        sum = result[0]
        for k in result:
            sum ^= k

        if sum == 0:
            return IMAGE_FORMAT[i], result[0]


def decode_image_data(data: bytes, key: int) -> bytes:
    image_data = []
    for byte in data:
        image_data.append(byte ^ key)
    return bytes(image_data)


def decode_image(src_file: str, output_path: str = ".") -> typing.Tuple[str, str]:
    src_file = pathlib.Path(src_file)
    output_path = pathlib.Path(output_path)
    dat_filename = src_file.name.replace(".dat", "")
    with open(src_file, "rb") as dat_file:
        data = dat_file.read()

    suffix, key = get_image_info(data)
    image_data = decode_image_data(data, key)

    image_filename = output_path / f"{dat_filename}.{suffix}"
    with open(image_filename, "wb") as f:
        f.write(image_data)

    return str(src_file.absolute()), str(image_filename.absolute())
