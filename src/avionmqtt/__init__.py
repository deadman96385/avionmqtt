from argparse import ArgumentParser
import asyncio
from typing import List
import yaml
import json
import aiomqtt
import logging
import csrmesh
from bleak import BleakClient, BleakScanner, BleakGATTCharacteristic
from bleak.exc import BleakError
from binascii import unhexlify, hexlify
from enum import Enum
import signal
import sys
from aiorun import run
from avionhttp import http_list_devices

MQTT_RETRY_INTERVAL = 5
CHARACTERISTIC_LOW = "c4edc000-9daf-11e3-8003-00025b000b00"
CHARACTERISTIC_HIGH = "c4edc000-9daf-11e3-8004-00025b000b00"

CAPABILITIES = {"dimming": {0, 162, 134, 97}, "color_temp": {0, 162, 134}}
PRODUCT_NAMES = {0: "Group", 162: "MicroEdge (HLB)", 97: "Smart Dimmer", 134: "Smart Bulb (A19)", 167: "Smart Switch"}


class Verb(Enum):
    WRITE = 0
    READ = 1
    INSERT = 2
    TRUNCATE = 3
    COUNT = 4
    DELETE = 5
    PING = 6
    SYNC = 7
    OTA = 8
    PUSH = 11
    SCAN_WIFI = 12
    CANCEL_DATASTREAM = 13
    UPDATE = 16
    TRIM = 17
    DISCONNECT_OTA = 18
    UNREGISTER = 20
    MARK = 21
    REBOOT = 22
    RESTART = 23
    OPEN_SSH = 32
    NONE = 255


class Noun(Enum):
    DIMMING = 10
    FADE_TIME = 25
    COUNTDOWN = 9
    DATE = 21
    TIME = 22
    SCHEDULE = 7
    GROUPS = 3
    SUNRISE_SUNSET = 6
    ASSOCIATION = 27
    WAKE_STATUS = 28
    COLOR = 29
    CONFIG = 30
    WIFI_NETWORKS = 31
    DIMMING_TABLE = 17
    ASSOCIATED_WIFI_NETWORK = 32
    ASSOCIATED_WIFI_NETWORK_STATUS = 33
    SCENES = 34
    SCHEDULE_2 = 35
    RAB_IP = 36
    RAB_ENV = 37
    RAB_CONFIG = 38
    THERMOMETER = 39
    FIRMWARE_VERSION = 40
    LUX_VALUE = 41
    TEST_MODE = 42
    HARCODED_STRING = 43
    RAB_MARKS = 44
    MOTION_SENSOR = 45
    ALS_DIMMING = 46
    ASSOCIATION_2 = 48
    RTC_SUN_RISE_SET_TABLE = 71
    RTC_DATE = 72
    RTC_TIME = 73
    RTC_DAYLIGHT_SAVING_TIME_TABLE = 74
    AVION_SENSOR = 91
    NONE = 255


def settings_get(file: str):
    with open(file) as stream:
        try:
            return yaml.safe_load(stream)
        except yaml.YAMLError as exc:
            print(exc)


async def mqtt_register(mqtt: aiomqtt.Client, entity: dict):
    product_id = entity["product_id"]
    pid = entity["pid"]
    avid = entity["avid"]
    name = entity["name"]

    # https://www.home-assistant.io/integrations/light.mqtt/
    config = {
        "component": "light",
        "object_id": f"avid_{avid}",
        "unique_id": pid,
        "schema": "json",
        "payload_off": "OFF",
        "payload_on": "ON",
        "brightness": product_id in CAPABILITIES["dimming"],
        "color_mode": product_id in CAPABILITIES["color_temp"],
        "effect": False,
        "retain": True,
        "state_topic": f"hmd/light/avid/{avid}/state",
        "json_attributes_topic": f"hmd/light/avid/{avid}/attributes",
        "command_topic": f"hmd/light/avid/{avid}/command",
        "device": {
            "identifiers": [pid],
            "name": name,
            "manufacturer": "Avi-on",
            "model": PRODUCT_NAMES.get(product_id, "Unknown"),
            "serial_number": pid,
        },
    }

    if product_id in CAPABILITIES["color_temp"]:
        config["supported_color_modes"] = ["color_temp"]
    await mqtt.publish(
        f"homeassistant/light/avid_{avid}/config",
        json.dumps(config),
    )


async def mqtt_register_category(settings: dict, list: List[dict], mqtt: aiomqtt.Client):
    if settings["import"]:
        include = settings.get("include", None)
        exclude = settings.get("exclude", {})
        print("exclude")
        print(exclude)
        for entity in list:
            pid = entity["pid"]
            if (include is not None and pid in include) or pid not in exclude:
                await mqtt_register(mqtt, entity)


async def mqtt_register_lights(settings: dict, location: dict, mqtt: aiomqtt.Client):
    await mqtt_register_category(settings["groups"], location["groups"], mqtt)

    if settings["devices"].get("exclude_in_group"):
        exclude = settings["devices"].get("exclude", set())
        for group in location["groups"]:
            devices = group["devices"]
            exclude |= set(devices)
        settings["devices"]["exclude"] = exclude
    await mqtt_register_category(settings["devices"], location["devices"], mqtt)
    if "all" in settings:
        await mqtt_register(mqtt, {"pid": "avion_all", "product_id": 0, "avid": 0, "name": settings["all"]["name"]})


def create_packet(target_id: int, verb: Verb, noun: Noun, value_bytes: bytearray) -> bytes:
    if target_id < 32896:
        group_id = target_id
        target_id = 0
    else:
        group_id = 0

    target_bytes = bytearray(target_id.to_bytes(2, byteorder="big"))
    group_bytes = bytearray(group_id.to_bytes(2, byteorder="big"))
    return bytes(
        [
            target_bytes[1],
            target_bytes[0],
            0x73,
            verb.value,
            noun.value,
            group_bytes[0],
            group_bytes[1],
            0,  # id
            *value_bytes,
            0,
            0,
        ]
    )


async def mesh_write_gatt(mesh: BleakClient, packet: bytes, key: str) -> bool:
    print("-".join(map(lambda b: format(b, "02x"), packet)))

    csrpacket = csrmesh.crypto.make_packet(key, csrmesh.crypto.random_seq(), packet)
    low = csrpacket[:20]
    high = csrpacket[20:]
    await mesh.write_gatt_char(CHARACTERISTIC_LOW, low)
    await mesh.write_gatt_char(CHARACTERISTIC_HIGH, high)
    return True


def mesh_get_color_temp_packet(target_id: int, color: int) -> bytearray:
    return create_packet(
        target_id,
        Verb.WRITE,
        Noun.COLOR,
        bytes([0x01, *bytearray(color.to_bytes(2, byteorder="big"))]),
    )


def mesh_get_brightness_packet(target_id: int, brightness: int) -> bytearray:
    return create_packet(target_id, Verb.WRITE, Noun.DIMMING, bytes([brightness, 0, 0]))


async def mesh_send(avid: int, raw_payload: str, mqtt: aiomqtt.Client, mesh: BleakClient, key: str) -> bool:
    payload = json.loads(raw_payload)
    if "brightness" in payload:
        packet = mesh_get_brightness_packet(avid, payload["brightness"])
    elif "color_temp" in payload:
        mired = payload["color_temp"]
        kelvin = (int)(1000000 / mired)
        print(f"mesh: Converting mired({mired}) to kelvin({kelvin})")
        packet = mesh_get_color_temp_packet(avid, kelvin)
    elif "state" in payload:
        packet = mesh_get_brightness_packet(avid, 255 if payload["state"] == "ON" else 0)
    else:
        print("mesh: Unknown payload")
        return False

    if await mesh_write_gatt(mesh, packet, key):
        print("mesh: Acknowedging directly")

        parsed = mesh_parse_command(avid, packet)
        if parsed:
            await mqtt_send_state(mqtt, parsed)


async def mqtt_subscribe(mqtt: aiomqtt.Client, mesh: BleakClient, key: str):
    await mqtt.subscribe("hmd/light/avid/+/command")
    async for message in mqtt.messages:
        json = message.payload.decode()
        avid = int(message.topic.value.split("/")[3])
        print(f"mqtt: received {json} for {avid}")
        await mesh_send(avid, json, mqtt, mesh, key)


async def mqtt_send_state(mqtt: aiomqtt.Client, message: dict):
    # TODO: Only send update if we've actually registered this device
    print(f"mqtt: sending update for {message}")
    avid = message["avid"]
    state_topic = f"hmd/light/avid/{avid}/state"
    if "brightness" in message:
        brightness = message["brightness"]
        payload = {
            "state": "ON" if brightness != 0 else "OFF",
            "brightness": brightness,
        }
        await mqtt.publish(state_topic, json.dumps(payload))
    elif "color_temp" in message:
        color_temp = message["color_temp"]
        payload = {"color_temp": color_temp}
        await mqtt.publish(state_topic, json.dumps(payload))


async def mac_ordered_by_rssi():
    scanned_devices = await BleakScanner.discover(return_adv=True)
    sorted_devices = sorted(scanned_devices.items(), key=lambda d: d[1][1].rssi)
    sorted_devices.reverse()
    return [d[0].lower() for d in sorted_devices]


def mesh_parse_data(target_id: int, data: bytearray) -> dict:
    print(f"mesh: parsing data {data} from {target_id}")

    if data[0] == 0 and data[1] == 0:
        print(f"empty data")
        return

    verb = Verb(data[0])
    noun = Noun(data[1])

    if verb == Verb.WRITE:
        target_id = target_id if target_id else int.from_bytes(bytes([data[2], data[3]]), byteorder="big")
        value_bytes = data[4:]
    else:
        value_bytes = data[2:]

    print(f"mesh: target_id({target_id}), verb({verb}), noun({noun}), value:{value_bytes})")

    if noun == Noun.DIMMING:
        brightness = int.from_bytes(value_bytes[1:2], byteorder="big")
        return {"avid": target_id, "brightness": brightness}
    elif noun == Noun.COLOR:
        temp = int.from_bytes(value_bytes[2:4], byteorder="big")
        return {"avid": target_id, "color_temp": temp}

    else:
        print(f"unknown noun {noun}")


# BLEBridge.decryptMessage
def mesh_parse_command(source: int, data: bytearray):
    hex = "-".join(map(lambda b: format(b, "01x"), data))
    print(f"mesh: parsing notification {hex}")
    if data[2] == 0x73:
        if data[0] == 0x0 and data[1] == 0x80:
            return mesh_parse_data(source, data[3:])
        else:
            return mesh_parse_data(int.from_bytes(bytes([data[1], data[0]]), byteorder="big"), data[3:])
    else:
        print(f"Unable to handle {data[2]}")


async def mesh_read_all(mesh: BleakClient, key: str):
    packet = create_packet(0, Verb.READ, Noun.DIMMING, bytearray(3))
    await mesh_write_gatt(mesh, packet, key)


async def mesh_subscribe(mqtt: aiomqtt.Client, mesh: BleakClient, key: str):
    async def cb(charactheristic: BleakGATTCharacteristic, data: bytearray):
        if charactheristic.uuid == CHARACTERISTIC_LOW:
            mesh.low_bytes = data
        elif charactheristic.uuid == CHARACTERISTIC_HIGH:
            encrypted = bytes([*mesh.low_bytes, *data])
            decoded = csrmesh.crypto.decrypt_packet(key, encrypted)
            parsed = mesh_parse_command(decoded["source"], decoded["decpayload"])
            if parsed:
                await mqtt_send_state(mqtt, parsed)

    await mesh.start_notify(CHARACTERISTIC_LOW, cb)
    await mesh.start_notify(CHARACTERISTIC_HIGH, cb)
    print("mesh: reading all")
    await mesh_read_all(mesh, key)


async def main():
    parser = ArgumentParser()
    parser.add_argument("-s", "--settings", dest="settings", help="yaml file to read settings from", metavar="FILE")
    args = parser.parse_args()

    settings = settings_get(args.settings)
    avion_settings = settings["avion"]
    mqtt_settings = settings["mqtt"]

    print("avion: Fetching devices")
    locations = await http_list_devices(avion_settings["email"], avion_settings["password"])
    assert len(locations) == 1
    location = locations[0]

    target_devices = [d["mac_address"].lower() for d in location["devices"]]

    mqtt = aiomqtt.Client(
        hostname=mqtt_settings["host"],
        username=mqtt_settings["username"],
        password=mqtt_settings["password"],
    )

    running = True
    # connect to mqtt
    while running:
        try:
            print("mqtt: Connecting to broker")
            async with mqtt:
                # register the lights
                await mqtt_register_lights(settings, location, mqtt)
                # now connect the mesh
                print("mesh: Connecting to mesh")
                while running:
                    try:
                        mesh = None
                        print("mesh: Scanning for devices")
                        for mac in set(await mac_ordered_by_rssi()).intersection(target_devices):
                            print(f"mesh: connecting to {mac}")
                            try:
                                ble_device = await BleakScanner.find_device_by_address(mac)
                                print(ble_device)
                                if ble_device is None:
                                    print(f"mesh: Could not find {mac}, moving on to the next device")
                                    continue
                                mesh = BleakClient(mac)
                                await mesh.connect()
                            except BleakError as e:
                                print(f"mesh: Error connecting to {mac}")
                                continue
                            except Exception as e:
                                print(f"mesh: Error connecting to {mac}")
                                continue
                            print(f"mesh: Connected to {mac}")
                            key = csrmesh.crypto.generate_key(
                                location["passphrase"].encode("ascii") + b"\x00\x4d\x43\x50"
                            )
                            # subscribe to updates from the mesh
                            await mesh_subscribe(mqtt, mesh, key)
                            # subscribe to commands from mqtt (this also keeps the loop going)
                            await mqtt_subscribe(mqtt, mesh, key)

                    except asyncio.CancelledError:
                        running = False

                    except BleakError as e:
                        print("mesh: Error connecting to device")

                    except Exception as e:
                        print("mesh: Exception")
                        # this isn't printing
                        print(e)

                    finally:
                        print("mesh: Done")
                        if mesh and mesh.is_connected:
                            await mesh.disconnect()
                            print(f"mesh: Disconnected from {mac}")

        except aiomqtt.MqttError:
            print(f"mqtt: Connection lost; Reconnecting in {MQTT_RETRY_INTERVAL} seconds ...")
            await asyncio.sleep(MQTT_RETRY_INTERVAL)
        finally:
            print("mqtt: Done")


# create a new event loop (low-level api)
run(main())
