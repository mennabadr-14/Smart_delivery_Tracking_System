
import json
import ssl
import time
import threading

import board
import busio
import paho.mqtt.client as mqtt
from adafruit_pn532.i2c import PN532_I2C
from gpiozero import LED, DigitalInputDevice, Servo

# ===================== CONFIG =====================
HIVEMQ_HOST = "e07d0632ae57495fb4ffdf645ad8bc1c.s1.eu.hivemq.cloud"   # غيّرها
HIVEMQ_PORT = 8883
HIVEMQ_USER = "menna"                        # غيّرها
HIVEMQ_PASS = "12345678"                        # غيّرها

TOPIC_STATUS = "smartdelivery/status"   # آخر حالة (retained)
TOPIC_EVENTS = "smartdelivery/events"   # كل الأحداث

DRIVER_UID = "B359B834"    # كارد أحمد
PACKAGE_UID = "7378BD02"   # كارد الباكدج

PIN_LED = 23
PIN_SERVO = 18
PIN_IR = 17

# أغلب موديولات IR بتدي LOW لما تشوف حاجة. لو موديولك بالعكس خليها False
IR_ACTIVE_LOW = True

# Servo values: من -1 (أقصى ناحية) لـ 1 (أقصى ناحية تانية)، 0 = النص
DOOR_CLOSED_VALUE = -1
DOOR_OPEN_VALUE = 0.0      # 0 = تقريباً 90 درجة. غيّرها حسب ميكانيكا الباب

IR_CHECK_TIMEOUT = 5       # ثواني نستنى فيها الـ IR يشوف الباكدج
PACKAGE_STEP_TIMEOUT = 30  # ثواني نستنى كارد الباكدج قبل ما نرجع لخطوة الدرايفر
# ==================================================

# ---------- States ----------
WAIT_DRIVER = "WAIT_DRIVER"
WAIT_PACKAGE = "WAIT_PACKAGE"
STOPPED = "STOPPED"

state = WAIT_DRIVER
step_started = time.time()
state_lock = threading.Lock()
mqtt_lock = threading.Lock()
stop_event = threading.Event()

# ---------- Hardware ----------
led = LED(PIN_LED)
ir = DigitalInputDevice(PIN_IR, pull_up=IR_ACTIVE_LOW)  # is_active = الباكدج موجودة
servo = Servo(
    PIN_SERVO,
    initial_value=None,
    min_pulse_width=0.5 / 1000,
    max_pulse_width=2.5 / 1000,
)
servo.detach()  # السيرفو واقف ومفيش إشارة لحد ما يجي دوره في الفلو

i2c = busio.I2C(board.SCL, board.SDA)
pn532 = PN532_I2C(i2c, debug=False)
pn532.SAM_configuration()


# ---------- MQTT ----------
def make_client():
    try:
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="smart_delivery_pi")
    except AttributeError:  # paho-mqtt 1.x
        c = mqtt.Client(client_id="smart_delivery_pi")
    c.username_pw_set(HIVEMQ_USER, HIVEMQ_PASS)
    c.tls_set(tls_version=ssl.PROTOCOL_TLS_CLIENT)
    c.will_set(TOPIC_STATUS, json.dumps({"status": "offline"}), qos=1, retain=True)
    return c


client = make_client()


def publish(status, extra=None, retain=True):
    payload = {"status": status, "time": time.strftime("%Y-%m-%d %H:%M:%S")}
    if extra:
        payload.update(extra)
    msg = json.dumps(payload)
    with mqtt_lock:
        client.publish(TOPIC_STATUS, msg, qos=1, retain=retain)
        client.publish(TOPIC_EVENTS, msg, qos=1)
    print(f"[MQTT] {status}")


# ---------- Helpers ----------
def open_door():
    servo.value = DOOR_OPEN_VALUE
    time.sleep(0.7)
    servo.detach()  # يوقف الإشارة عشان مايرعشش


def close_door():
    servo.value = DOOR_CLOSED_VALUE
    time.sleep(0.7)
    servo.detach()


def blink_error():
    led.blink(on_time=0.15, off_time=0.15, n=6, background=False)


def read_uid(timeout=0.5):
    """يرجع UID كـ 'B359B834' أو None."""
    uid = pn532.read_passive_target(timeout=timeout)
    if uid is None:
        return None
    return "".join(f"{b:02X}" for b in uid)


def wait_card_removed():
    time.sleep(0.8)
    misses = 0
    while misses < 3 and not stop_event.is_set():
        misses = misses + 1 if read_uid(timeout=0.2) is None else 0


# ---------- Flow ----------
def reset_to_driver_step():
    global state, step_started
    with state_lock:
        state = WAIT_DRIVER
        step_started = time.time()
    led.off()
    publish("waiting_for_driver_card")


def stop_system():
    """بعد in_transit: النظام يقف ومايقراش كروت ومايبعتش حاجة تاني (من غير أي رسالة)."""
    global state
    with state_lock:
        state = STOPPED
    led.off()


def handle_driver_step(uid):
    global state, step_started
    if uid == DRIVER_UID:
        led.on()
        publish("driver_card_authorized", {"driver": "Ahmed", "uid": uid})
        with state_lock:
            state = WAIT_PACKAGE
            step_started = time.time()
        publish("waiting_for_package_card")
    else:
        publish("wrong_driver_card", {"uid": uid})
        blink_error()


def handle_package_step(uid):
    if uid != PACKAGE_UID:
        publish("wrong_card", {"uid": uid})
        blink_error()
        led.on()  # الدرايفر لسه authorized
        return

    publish("package_card_authorized", {"uid": uid})

    open_door()
    publish("door_open")

    found = False
    deadline = time.time() + IR_CHECK_TIMEOUT
    while time.time() < deadline:
        if ir.is_active:
            found = True
            break
        time.sleep(0.1)

    if found:
        publish("loaded")
        close_door()
        publish("door_closed")
        publish("in_transit")
        stop_system()          # آخر حاجة: يقف هنا
    else:
        publish("package_missing")
        blink_error()
        close_door()
        publish("door_closed")
        reset_to_driver_step() # package missing: النظام يكمل شغال عادي


# ---------- Threads ----------
def timeout_watchdog():
    """Thread بيرجّع النظام لخطوة الدرايفر لو كارد الباكدج اتأخر."""
    while not stop_event.is_set():
        expired = False
        with state_lock:
            if state == WAIT_PACKAGE and time.time() - step_started > PACKAGE_STEP_TIMEOUT:
                expired = True
        if expired:
            publish("timeout")
            reset_to_driver_step()
        time.sleep(1)


def nfc_loop():
    while not stop_event.is_set():
        with state_lock:
            if state == STOPPED:
                time.sleep(0.5)
                continue
        uid = read_uid()
        if uid:
            print(f"[NFC] UID = {uid}")
            with state_lock:
                current = state
            if current == STOPPED:
                continue
            if current == WAIT_DRIVER:
                handle_driver_step(uid)
            else:
                handle_package_step(uid)
            wait_card_removed()
        time.sleep(0.05)


def main():
    client.connect(HIVEMQ_HOST, HIVEMQ_PORT, keepalive=60)
    client.loop_start()

    # close_door()  # معمولة تعليق عشان السيرفو يفضل واقف عند التشغيل
    reset_to_driver_step()
    print("System ready. Waiting for driver card...")

    threads = [
        threading.Thread(target=nfc_loop, daemon=True),
        threading.Thread(target=timeout_watchdog, daemon=True),
    ]
    for t in threads:
        t.start()

    try:
        while not stop_event.is_set():
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        publish("offline")
        client.loop_stop()
        client.disconnect()
        servo.close()
        led.close()
        ir.close()


if __name__ == "__main__":
    main()