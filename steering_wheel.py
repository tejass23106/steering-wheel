import math
import threading
import time
import cv2
import mediapipe as mp
import numpy as np
try:
    import keyboard as _kb_backend
    _USE_KEYBOARD_LIB = True
except ImportError:
    _kb_backend = None
    _USE_KEYBOARD_LIB = False
import pyautogui
pyautogui.PAUSE = 0
pyautogui.FAILSAFE = False
def key_down(key_name):
    if _USE_KEYBOARD_LIB:
        _kb_backend.press(key_name)
    else:
        pyautogui.keyDown(key_name)
def key_up(key_name):
    if _USE_KEYBOARD_LIB:
        _kb_backend.release(key_name)
    else:
        pyautogui.keyUp(key_name)
CAM_INDEX = 0
FRAME_W, FRAME_H = (960, 720)
PROCESS_SCALE = 0.6
TURN_ENTER_DEG = 10
TURN_EXIT_DEG = 5.5
TURN_MAX_DEG = 90
ACCEL_Y_RATIO = 0.72
BRAKE_Y_RATIO = 0.9
KEY_LEFT = 'left'
KEY_RIGHT = 'right'
KEY_UP = 'up'
KEY_DOWN = 'down'
ONE_EURO_MIN_CUTOFF = 1.3
ONE_EURO_BETA = 0.65
POINT_MIN_CUTOFF = 1.0
POINT_BETA = 0.7
AUTO_CALIBRATE_DELAY_SEC = 2.5
GESTURE_CALIBRATE_DISTANCE_RATIO = 0.22
GESTURE_CALIBRATE_HOLD_SEC = 0.5
HAND_LOST_GRACE_FRAMES = 8
KEY_DEBOUNCE_SEC = 0.12
cv2.setUseOptimized(True)
cv2.setNumThreads(1)
mp_hands = mp.solutions.hands
mp_draw = mp.solutions.drawing_utils
hands = mp_hands.Hands(max_num_hands=2, model_complexity=1, min_detection_confidence=0.7, min_tracking_confidence=0.6)
class OneEuroFilter:
    def __init__(self, mincutoff=1.0, beta=0.0, dcutoff=1.0):
        self.mincutoff = mincutoff
        self.beta = beta
        self.dcutoff = dcutoff
        self.x_prev = None
        self.dx_prev = 0.0
        self.t_prev = None
    @staticmethod
    def _alpha(cutoff, dt):
        tau = 1.0 / (2 * math.pi * cutoff)
        return 1.0 / (1.0 + tau / dt)
    def filter(self, x, t):
        if self.t_prev is None:
            self.t_prev = t
            self.x_prev = x
            return x
        dt = max(t - self.t_prev, 1e-06)
        dx = (x - self.x_prev) / dt
        a_d = self._alpha(self.dcutoff, dt)
        dx_hat = a_d * dx + (1 - a_d) * self.dx_prev
        cutoff = self.mincutoff + self.beta * abs(dx_hat)
        a = self._alpha(cutoff, dt)
        x_hat = a * x + (1 - a) * self.x_prev
        self.x_prev = x_hat
        self.dx_prev = dx_hat
        self.t_prev = t
        return x_hat
    def reset(self):
        self.x_prev = None
        self.dx_prev = 0.0
        self.t_prev = None
class ThreadedCamera:
    def __init__(self, src, width, height):
        self.cap = cv2.VideoCapture(src, cv2.CAP_DSHOW)
        if not self.cap.isOpened():
            self.cap = cv2.VideoCapture(src)
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        self.lock = threading.Lock()
        self.ret, self.frame = self.cap.read() if self.cap.isOpened() else (False, None)
        self.stopped = False
        self.thread = threading.Thread(target=self._update, daemon=True)
        self.thread.start()
    def isOpened(self):
        return self.cap.isOpened()
    def _update(self):
        while not self.stopped:
            try:
                ret, frame = self.cap.read()
            except cv2.error:
                ret, frame = (False, None)
            with self.lock:
                self.ret, self.frame = (ret, frame)
    def read(self):
        with self.lock:
            if self.frame is None:
                return (False, None)
            return (self.ret, self.frame.copy())
    def release(self):
        self.stopped = True
        self.thread.join(timeout=1.0)
        self.cap.release()
def get_wrist_points(results, frame_w, frame_h, prev_left=None, prev_right=None):
    if not results.multi_hand_landmarks:
        return (None, None)
    points = []
    for hand_landmarks in results.multi_hand_landmarks:
        wrist = hand_landmarks.landmark[mp_hands.HandLandmark.WRIST]
        x, y = (int(wrist.x * frame_w), int(wrist.y * frame_h))
        points.append((x, y))
    if len(points) < 2:
        return (None, None)
    p1, p2 = (points[0], points[1])
    if prev_left is not None and prev_right is not None:
        dist_a = math.hypot(p1[0] - prev_left[0], p1[1] - prev_left[1]) + math.hypot(p2[0] - prev_right[0], p2[1] - prev_right[1])
        dist_b = math.hypot(p2[0] - prev_left[0], p2[1] - prev_left[1]) + math.hypot(p1[0] - prev_right[0], p1[1] - prev_right[1])
        return (p1, p2) if dist_a <= dist_b else (p2, p1)
    points.sort(key=lambda p: p[0])
    return (points[0], points[1])
def angle_between(left_point, right_point):
    dx = right_point[0] - left_point[0]
    dy = right_point[1] - left_point[1]
    return math.degrees(math.atan2(dy, dx))
def next_turn_state(prev_state, angle):
    if prev_state == 'STRAIGHT':
        if angle < -TURN_ENTER_DEG:
            return 'LEFT'
        if angle > TURN_ENTER_DEG:
            return 'RIGHT'
        return 'STRAIGHT'
    if prev_state == 'LEFT':
        return 'STRAIGHT' if angle > -TURN_EXIT_DEG else 'LEFT'
    if prev_state == 'RIGHT':
        return 'STRAIGHT' if angle < TURN_EXIT_DEG else 'RIGHT'
    return 'STRAIGHT'
def draw_steering_wheel(frame, left_point, right_point, angle):
    mx = (left_point[0] + right_point[0]) // 2
    my = (left_point[1] + right_point[1]) // 2
    dx = right_point[0] - left_point[0]
    dy = right_point[1] - left_point[1]
    radius = max(int(math.hypot(dx, dy) / 2), 45)
    display_angle = max(-TURN_MAX_DEG, min(TURN_MAX_DEG, angle))
    cv2.circle(frame, (mx, my), radius, (25, 25, 25), 16, lineType=cv2.LINE_AA)
    cv2.circle(frame, (mx, my), radius + 6, (70, 70, 70), 2, lineType=cv2.LINE_AA)
    cv2.circle(frame, (mx, my), radius - 6, (70, 70, 70), 2, lineType=cv2.LINE_AA)
    cv2.circle(frame, (mx, my), radius, (90, 90, 200), 3, lineType=cv2.LINE_AA)
    spoke_half_width = max(int(radius * 0.11), 8)
    hub_radius = max(int(radius * 0.28), 22)
    for offset_deg in (0, 120, 240):
        a = math.radians(display_angle + offset_deg)
        dir_x, dir_y = (math.cos(a), math.sin(a))
        perp_x, perp_y = (-dir_y, dir_x)
        inner = (mx + hub_radius * dir_x, my + hub_radius * dir_y)
        outer = (mx + (radius - 4) * dir_x, my + (radius - 4) * dir_y)
        p1 = (int(inner[0] + perp_x * spoke_half_width), int(inner[1] + perp_y * spoke_half_width))
        p2 = (int(inner[0] - perp_x * spoke_half_width), int(inner[1] - perp_y * spoke_half_width))
        p3 = (int(outer[0] - perp_x * spoke_half_width * 0.5), int(outer[1] - perp_y * spoke_half_width * 0.5))
        p4 = (int(outer[0] + perp_x * spoke_half_width * 0.5), int(outer[1] + perp_y * spoke_half_width * 0.5))
        pts = np.array([p1, p2, p3, p4], dtype=np.int32)
        cv2.fillPoly(frame, [pts], (60, 60, 60), lineType=cv2.LINE_AA)
        cv2.polylines(frame, [pts], True, (110, 110, 110), 1, lineType=cv2.LINE_AA)
    cv2.circle(frame, (mx, my), hub_radius, (20, 20, 20), -1, lineType=cv2.LINE_AA)
    cv2.circle(frame, (mx, my), hub_radius, (150, 150, 150), 2, lineType=cv2.LINE_AA)
    cv2.circle(frame, (mx, my), max(int(hub_radius * 0.4), 6), (90, 90, 200), -1, lineType=cv2.LINE_AA)
    cv2.circle(frame, left_point, 15, (255, 140, 0), -1, lineType=cv2.LINE_AA)
    cv2.circle(frame, left_point, 15, (255, 255, 255), 2, lineType=cv2.LINE_AA)
    cv2.circle(frame, right_point, 15, (0, 255, 0), -1, lineType=cv2.LINE_AA)
    cv2.circle(frame, right_point, 15, (255, 255, 255), 2, lineType=cv2.LINE_AA)
def draw_lock_meter(frame, angle):
    h, w, _ = frame.shape
    cx, cy = (w // 2, h - 20)
    r = 70
    cv2.ellipse(frame, (cx, cy), (r, r), 0, 180, 360, (90, 90, 90), 4)
    clamped = max(-TURN_MAX_DEG, min(TURN_MAX_DEG, angle))
    lock_pct = int(abs(clamped) / TURN_MAX_DEG * 100)
    needle_angle_deg = 180 - (clamped + 90)
    a = math.radians(needle_angle_deg)
    tip = (int(cx + r * math.cos(a)), int(cy - abs(r * math.sin(a))))
    needle_color = (0, 255, 0) if abs(clamped) < TURN_ENTER_DEG else (0, 165, 255) if lock_pct < 70 else (0, 0, 255)
    cv2.line(frame, (cx, cy), tip, needle_color, 3)
    cv2.circle(frame, (cx, cy), 6, (255, 255, 255), -1)
    cv2.putText(frame, f'LOCK: {lock_pct}%', (cx - 60, cy - r - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (255, 255, 255), 2)
def draw_overlay(frame, left_point, right_point, angle, state):
    h, w, _ = frame.shape
    if left_point and right_point:
        draw_steering_wheel(frame, left_point, right_point, angle)
    cv2.putText(frame, '<-- LEFT', (30, h - 210), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 100, 255), 2)
    cv2.putText(frame, 'RIGHT -->', (w - 220, h - 210), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)
    label_color = {'LEFT': (0, 100, 255), 'RIGHT': (0, 255, 0), 'STRAIGHT': (255, 255, 255)}[state]
    cv2.putText(frame, state, (w // 2 - 100, h - 160), cv2.FONT_HERSHEY_SIMPLEX, 1.3, label_color, 3)
    if angle is not None:
        cv2.putText(frame, f'{angle:+.1f} deg', (30, h - 40), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (255, 255, 255), 2)
        draw_lock_meter(frame, angle)
def main():
    if _USE_KEYBOARD_LIB:
        print("Using 'keyboard' library for key events (low latency).")
    else:
        print("'keyboard' library not found - using pyautogui instead.")
        print('For best responsiveness, you can: pip install keyboard')
    cam = ThreadedCamera(CAM_INDEX, FRAME_W, FRAME_H)
    if not cam.isOpened():
        print('ERROR: Could not open webcam. Close any other app using it (Zoom, Teams, another Python instance) and check CAM_INDEX.')
        return
    angle_filter = OneEuroFilter(mincutoff=ONE_EURO_MIN_CUTOFF, beta=ONE_EURO_BETA)
    point_filters = {'lx': OneEuroFilter(mincutoff=POINT_MIN_CUTOFF, beta=POINT_BETA), 'ly': OneEuroFilter(mincutoff=POINT_MIN_CUTOFF, beta=POINT_BETA), 'rx': OneEuroFilter(mincutoff=POINT_MIN_CUTOFF, beta=POINT_BETA), 'ry': OneEuroFilter(mincutoff=POINT_MIN_CUTOFF, beta=POINT_BETA)}
    last_left_point = None
    last_right_point = None
    frames_since_hands_seen = 0
    current_turn_key = None
    pending_turn_key = None
    pending_turn_since = 0.0
    accel_held = False
    brake_held = False
    pending_accel = False
    pending_accel_since = 0.0
    pending_brake = False
    pending_brake_since = 0.0
    turn_state = 'STRAIGHT'
    angle_offset = 0.0
    has_auto_calibrated = False
    hands_first_seen_at = None
    gesture_close_since = None
    calibration_pending = False
    pending_since = None
    calibrated_flash_until = 0.0
    prev_time = time.time()
    consecutive_failures = 0
    MAX_FAILURES = 60
    MAX_RECONNECT_ATTEMPTS = 5
    reconnect_attempts = 0
    window_name = 'Virtual Steering Wheel - Q: quit  C: calibrate center'
    cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(window_name, FRAME_W, FRAME_H)
    try:
        while True:
            try:
                ok, frame = cam.read()
            except Exception as e:
                print(f'Camera read error: {e}')
                ok, frame = (False, None)
            if not ok or frame is None:
                consecutive_failures += 1
                if consecutive_failures > MAX_FAILURES:
                    reconnect_attempts += 1
                    if reconnect_attempts > MAX_RECONNECT_ATTEMPTS:
                        print(f'Lost connection to webcam - stopping after {MAX_RECONNECT_ATTEMPTS} reconnect attempts.')
                        break
                    print(f'Webcam not responding - attempting reconnect ({reconnect_attempts}/{MAX_RECONNECT_ATTEMPTS})...')
                    cam.release()
                    time.sleep(0.5)
                    cam = ThreadedCamera(CAM_INDEX, FRAME_W, FRAME_H)
                    consecutive_failures = 0
                    if not cam.isOpened():
                        continue
                time.sleep(0.01)
                continue
            consecutive_failures = 0
            try:
                frame = cv2.flip(frame, 1)
                h, w, _ = frame.shape
                now_t = time.time()
                small = cv2.resize(frame, None, fx=PROCESS_SCALE, fy=PROCESS_SCALE, interpolation=cv2.INTER_AREA)
                rgb_small = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
                rgb_small.flags.writeable = False
                results = hands.process(rgb_small)
                if results.multi_hand_landmarks:
                    for hand_landmarks in results.multi_hand_landmarks:
                        mp_draw.draw_landmarks(frame, hand_landmarks, mp_hands.HAND_CONNECTIONS)
                left_point, right_point = get_wrist_points(results, w, h, prev_left=last_left_point, prev_right=last_right_point)
                if left_point and right_point:
                    lx = point_filters['lx'].filter(left_point[0], now_t)
                    ly = point_filters['ly'].filter(left_point[1], now_t)
                    rx = point_filters['rx'].filter(right_point[0], now_t)
                    ry = point_filters['ry'].filter(right_point[1], now_t)
                    left_point = (int(lx), int(ly))
                    right_point = (int(rx), int(ry))
                    last_left_point, last_right_point = (left_point, right_point)
                    frames_since_hands_seen = 0
                elif last_left_point and frames_since_hands_seen < HAND_LOST_GRACE_FRAMES:
                    frames_since_hands_seen += 1
                    left_point, right_point = (last_left_point, last_right_point)
                else:
                    frames_since_hands_seen += 1
                    last_left_point = last_right_point = None
                angle = None
                accelerating = False
                braking = False
                just_calibrated = False
                if left_point and right_point:
                    hand_dist = math.hypot(right_point[0] - left_point[0], right_point[1] - left_point[1])
                    if hands_first_seen_at is None:
                        hands_first_seen_at = now_t
                    if not has_auto_calibrated and now_t - hands_first_seen_at >= AUTO_CALIBRATE_DELAY_SEC:
                        angle_offset = angle_between(left_point, right_point)
                        angle_filter.reset()
                        has_auto_calibrated = True
                        just_calibrated = True
                    if not calibration_pending:
                        if hand_dist < w * GESTURE_CALIBRATE_DISTANCE_RATIO:
                            if gesture_close_since is None:
                                gesture_close_since = now_t
                            elif now_t - gesture_close_since >= GESTURE_CALIBRATE_HOLD_SEC:
                                calibration_pending = True
                                gesture_close_since = None
                                pending_since = None
                        else:
                            gesture_close_since = None
                    elif hand_dist > w * GESTURE_CALIBRATE_DISTANCE_RATIO * 1.6:
                        if pending_since is None:
                            pending_since = now_t
                        elif now_t - pending_since >= GESTURE_CALIBRATE_HOLD_SEC:
                            angle_offset = angle_between(left_point, right_point)
                            angle_filter.reset()
                            calibration_pending = False
                            pending_since = None
                            just_calibrated = True
                    else:
                        pending_since = None
                    raw_angle = angle_between(left_point, right_point) - angle_offset
                    angle = angle_filter.filter(raw_angle, now_t)
                    turn_state = next_turn_state(turn_state, angle)
                    avg_y = (left_point[1] + right_point[1]) / 2
                    accelerating = avg_y < h * ACCEL_Y_RATIO
                    braking = avg_y > h * BRAKE_Y_RATIO
                else:
                    turn_state = 'STRAIGHT'
                    gesture_close_since = None
                desired_turn_key = KEY_LEFT if turn_state == 'LEFT' else KEY_RIGHT if turn_state == 'RIGHT' else None
                if desired_turn_key != pending_turn_key:
                    pending_turn_key = desired_turn_key
                    pending_turn_since = now_t
                elif pending_turn_key != current_turn_key and now_t - pending_turn_since >= KEY_DEBOUNCE_SEC:
                    if current_turn_key is not None:
                        key_up(current_turn_key)
                    if pending_turn_key is not None:
                        key_down(pending_turn_key)
                    current_turn_key = pending_turn_key
                if accelerating != pending_accel:
                    pending_accel = accelerating
                    pending_accel_since = now_t
                elif pending_accel != accel_held and now_t - pending_accel_since >= KEY_DEBOUNCE_SEC:
                    key_down(KEY_UP) if pending_accel else key_up(KEY_UP)
                    accel_held = pending_accel
                if braking != pending_brake:
                    pending_brake = braking
                    pending_brake_since = now_t
                elif pending_brake != brake_held and now_t - pending_brake_since >= KEY_DEBOUNCE_SEC:
                    key_down(KEY_DOWN) if pending_brake else key_up(KEY_DOWN)
                    brake_held = pending_brake
                draw_overlay(frame, left_point, right_point, angle, turn_state)
                line_y = int(h * ACCEL_Y_RATIO)
                cv2.line(frame, (0, line_y), (w, line_y), (100, 100, 100), 1)
                brake_line_y = int(h * BRAKE_Y_RATIO)
                cv2.line(frame, (0, brake_line_y), (w, brake_line_y), (100, 100, 255), 1)
                if braking:
                    accel_text, accel_color = ('BRAKING', (0, 0, 255))
                elif accelerating:
                    accel_text, accel_color = ('ACCELERATING', (0, 255, 0))
                else:
                    accel_text, accel_color = ('COASTING', (0, 165, 255))
                cv2.putText(frame, accel_text, (w // 2 - 110, 60), cv2.FONT_HERSHEY_SIMPLEX, 1.0, accel_color, 3)
                now = time.time()
                fps = 1 / (now - prev_time) if now != prev_time else 0
                prev_time = now
                cv2.putText(frame, f'FPS: {int(fps)}', (w - 150, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
                status = 'BOTH HANDS DETECTED' if left_point and right_point else 'SHOW BOTH HANDS'
                status_color = (0, 255, 0) if left_point and right_point else (0, 0, 255)
                cv2.putText(frame, status, (20, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, status_color, 2)
                if not has_auto_calibrated and hands_first_seen_at is not None:
                    remaining = AUTO_CALIBRATE_DELAY_SEC - (now_t - hands_first_seen_at)
                    if remaining > 0:
                        cv2.putText(frame, f'Hold pose - calibrating in {remaining:.1f}s', (w // 2 - 190, 90), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
                if gesture_close_since is not None:
                    held_for = now_t - gesture_close_since
                    pct = min(100, int(held_for / GESTURE_CALIBRATE_HOLD_SEC * 100))
                    cv2.putText(frame, f'Recalibrating... {pct}%', (w // 2 - 140, 90), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
                elif calibration_pending:
                    cv2.putText(frame, 'Move hands to your driving position and hold', (w // 2 - 260, 90), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (0, 255, 255), 2)
                if just_calibrated:
                    calibrated_flash_until = now_t + 1.0
                if now_t < calibrated_flash_until:
                    cv2.putText(frame, 'CALIBRATED', (w // 2 - 120, h // 2), cv2.FONT_HERSHEY_SIMPLEX, 1.4, (0, 255, 0), 4)
                cv2.putText(frame, '[Q] Quit   [C] Calibrate   or bring hands together to recalibrate', (20, h - 15), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (180, 180, 180), 1)
                cv2.imshow(window_name, frame)
            except Exception as e:
                print(f'Frame processing error (skipped this frame): {e}')
                continue
            key = cv2.waitKey(1) & 255
            if key == ord('q'):
                break
            elif key == ord('c') and left_point and right_point:
                angle_offset = angle_between(left_point, right_point)
                angle_filter.reset()
                calibrated_flash_until = time.time() + 1.0
                print(f'Calibrated - new center offset: {angle_offset:.1f} deg')
    except KeyboardInterrupt:
        print('Stopped by user (Ctrl+C).')
    finally:
        if current_turn_key is not None:
            key_up(current_turn_key)
        if accel_held:
            key_up(KEY_UP)
        if brake_held:
            key_up(KEY_DOWN)
        cam.release()
        cv2.destroyAllWindows()
        hands.close()
if __name__ == '__main__':
    main()
