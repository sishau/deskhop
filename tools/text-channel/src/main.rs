// ---------------------------------------------------------------------------
// deskhop-text-channel — DeskHop cross-board text transfer (firmware v0.80.3+)
// ---------------------------------------------------------------------------
// A background tray utility. Runs on BOTH machines connected to the DeskHop.
//
//   Ctrl+Alt+U : push the CURRENT CLIPBOARD content to the OTHER machine's
//                Pico (8 KB single-shot buffer). No keys are ever injected -
//                copy the text yourself first (^c, Ctrl+Shift+C, right-click).
//   Ctrl+Alt+D : pull the buffered text from THIS machine's Pico onto the
//                clipboard. Paste it yourself (^v, Ctrl+Shift+V, right-click).
//   Ctrl+Alt+0 : exit
//
// KVM-state watcher (firmware v0.80.6+): the side display's owner is picked
// with the KVM box button, which the firmware cannot see - but the host can:
// pressing the button drops/raises the phantom display, so SM_CMONITORS
// flips 2 <-> 1 (verified 2026-10-13). WM_DISPLAYCHANGE carries that change
// event-driven; after a 1 s debounce the matching layout profile is sent to
// the Pico as an absolute value (idempotent on the firmware side). A 5 s
// reconcile timer is the safety net for lost events / resume-from-sleep.
// Meaningful on the HOST machine only (the laptop always sees its phantom);
// harmless there, since the count never changes.
//
// Right-click the tray icon for an Exit menu.
//
// Uses only hid.dll / setupapi.dll / user32 / kernel32 / shell32 through
// hand-written FFI - no third-party crates, no AHK, no driver.
//
// Wire protocol (must match src/text_channel.c):
//   USB packet = report_id(6) + 0xAA + 0x55 + type + data[8] + xor(data[8])
//   TinyUSB strips the report id on SET_REPORT and fills it on GET_REPORT.
// ---------------------------------------------------------------------------

#![windows_subsystem = "windows"]

use std::sync::atomic::{AtomicIsize, AtomicU32, Ordering};

// ========================= Configuration =========================

const REPORT_ID: u8 = 6;
const VID_PID: &str = "vid_1209&pid_c000";
const IFACE_SUFFIX: &str = "mi_02";
const USB_PKT_LEN: usize = 13; // report id + 12-byte RAW packet
const MAX_TEXT: usize = 8191;  // firmware buffer is 8192 bytes

const TC_PUSH_BEGIN: u8 = 1; // data[0..2] = total length, little endian
const TC_PUSH_DATA: u8 = 2;  // data[0] = seq, data[1..8] = 7 bytes of text
const TC_PULL_REQ: u8 = 3;   // restart the read side
const TC_PULL_DATA: u8 = 5;  // data[0..7] = text, data[7] = 0x80|n on last chunk
const TC_PULL_EMPTY: u8 = 0xFF;
const TC_LAYOUT_SELECT: u8 = 4; // data[0] = target profile (0=HOST, 1=LAPTOP)

// KVM watcher tuning
const KVM_DEBOUNCE_MS: u32 = 1000;  // candidate count must hold this long
const KVM_RECONCILE_MS: u32 = 5000; // safety-net poll for lost events
const KVM_TIMER_DEBOUNCE: usize = 1;
const KVM_TIMER_RECONCILE: usize = 2;
const SM_CMONITORS: i32 = 80;

// ========================= Win32 constants =========================

const GENERIC_READ: u32 = 0x8000_0000;
const GENERIC_WRITE: u32 = 0x4000_0000;
const FILE_SHARE_READ: u32 = 1;
const FILE_SHARE_WRITE: u32 = 2;
const OPEN_EXISTING: u32 = 3;
const DIGCF_PRESENT: u32 = 0x2;
const DIGCF_DEVICEINTERFACE: u32 = 0x10;
const INVALID_HANDLE: isize = -1;

const WM_DESTROY: u32 = 0x0002;
const WM_COMMAND: u32 = 0x0111;
const WM_TIMER: u32 = 0x0113;
const WM_RBUTTONUP: u32 = 0x0205;
const WM_HOTKEY: u32 = 0x0312;
const WM_DISPLAYCHANGE: u32 = 0x007E;
const WM_APP: u32 = 0x8000;

const MOD_CONTROL: u32 = 0x2;
const MOD_ALT: u32 = 0x1;

const CF_UNICODETEXT: u32 = 13;
const GMEM_MOVEABLE: u32 = 0x2;

const NIM_ADD: u32 = 0;
const NIM_DELETE: u32 = 2;
const NIF_MESSAGE: u32 = 0x1;
const NIF_ICON: u32 = 0x2;
const NIF_TIP: u32 = 0x4;
const TRAY_CALLBACK: u32 = WM_APP + 1;

const MF_STRING: u32 = 0x0;
const TPM_RIGHTBUTTON: u32 = 0x2;

// ========================= FFI =========================

type Handle = isize;

#[repr(C)]
#[derive(Default, Clone, Copy)]
struct Guid {
    d0: u32,
    d1: u16,
    d2: u16,
    d3: [u8; 8],
}

#[repr(C)]
struct Msg {
    hwnd: Handle,
    message: u32,
    w_param: usize,
    l_param: isize,
    time: u32,
    pt: Point,
}

#[repr(C)]
#[derive(Default, Clone, Copy)]
struct Point {
    x: i32,
    y: i32,
}

#[repr(C)]
struct WndClassW {
    style: u32,
    wnd_proc: Option<extern "system" fn(Handle, u32, usize, isize) -> isize>,
    cls_extra: i32,
    wnd_extra: i32,
    instance: Handle,
    icon: Handle,
    cursor: Handle,
    background: Handle,
    menu_name: *const u16,
    class_name: *const u16,
}

#[repr(C)]
struct NotifyIconDataW {
    cb_size: u32,
    hwnd: Handle,
    uid: u32,
    uflags: u32,
    callback_msg: u32,
    hicon: Handle,
    sz_tip: [u16; 128],
    dw_state: u32,
    dw_state_mask: u32,
    sz_info: [u16; 256],
    u_timeout: u32, // union { uTimeout; uVersion; uState; }
    sz_info_title: [u16; 64],
    dw_info_flags: u32,
    guid_item: Guid,
    h_balloon_icon: Handle,
}

#[link(name = "hid")]
extern "system" {
    // BOOLEAN return = 1 byte
    fn HidD_GetHidGuid(guid: *mut Guid);
    fn HidD_SetOutputReport(h: Handle, buf: *const u8, len: u32) -> u8;
    fn HidD_GetInputReport(h: Handle, buf: *mut u8, len: u32) -> u8;
}

#[link(name = "setupapi")]
extern "system" {
    fn SetupDiGetClassDevsW(guid: *const Guid, enumerator: *const u16, hwnd: Handle, flags: u32) -> Handle;
    fn SetupDiEnumDeviceInterfaces(h: Handle, dev: *const u8, guid: *const Guid, idx: u32, data: *mut u8) -> i32;
    fn SetupDiGetDeviceInterfaceDetailW(
        h: Handle, data: *const u8, detail: *mut u8, detail_len: u32,
        required: *mut u32, dev_data: *mut u8,
    ) -> i32;
    fn SetupDiDestroyDeviceInfoList(h: Handle) -> i32;
}

#[link(name = "kernel32")]
extern "system" {
    fn CreateFileW(name: *const u16, access: u32, share: u32, sa: *const u8,
                   disp: u32, flags: u32, tmpl: Handle) -> Handle;
    fn GlobalAlloc(flags: u32, bytes: usize) -> Handle;
    fn GlobalLock(h: Handle) -> *mut u8;
    fn GlobalUnlock(h: Handle) -> i32;
    fn GlobalFree(h: Handle) -> Handle;
    fn GetModuleHandleW(name: *const u16) -> Handle;
    fn GetTickCount() -> u32;
}

#[link(name = "user32")]
extern "system" {
    fn RegisterHotKey(hwnd: Handle, id: i32, mods: u32, vk: u32) -> i32;
    fn GetMessageW(msg: *mut Msg, hwnd: Handle, min: u32, max: u32) -> i32;
    fn TranslateMessage(msg: *const Msg) -> i32;
    fn DispatchMessageW(msg: *const Msg) -> isize;
    fn PostQuitMessage(code: i32);
    fn OpenClipboard(hwnd: Handle) -> i32;
    fn CloseClipboard() -> i32;
    fn EmptyClipboard() -> i32;
    fn GetClipboardData(fmt: u32) -> Handle;
    fn SetClipboardData(fmt: u32, data: Handle) -> Handle;
    fn RegisterClassW(wc: *const WndClassW) -> u16;
    fn CreateWindowExW(ex: u32, class: *const u16, name: *const u16, style: u32,
                       x: i32, y: i32, w: i32, h: i32, parent: Handle, menu: Handle,
                       instance: Handle, param: *const u8) -> Handle;
    fn DefWindowProcW(hwnd: Handle, msg: u32, wp: usize, lp: isize) -> isize;
    fn LoadIconW(instance: Handle, name: *const u16) -> Handle;
    fn CreatePopupMenu() -> Handle;
    fn AppendMenuW(menu: Handle, flags: u32, id: usize, text: *const u16) -> i32;
    fn TrackPopupMenu(menu: Handle, flags: u32, x: i32, y: i32, reserved: i32, hwnd: Handle, rect: *const u8) -> i32;
    fn DestroyMenu(menu: Handle) -> i32;
    fn SetForegroundWindow(hwnd: Handle) -> i32;
    fn GetCursorPos(pt: *mut Point) -> i32;
    fn GetSystemMetrics(index: i32) -> i32;
    fn SetTimer(hwnd: Handle, id: usize, ms: u32, proc: isize) -> usize;
    fn KillTimer(hwnd: Handle, id: usize) -> i32;
}

#[link(name = "shell32")]
extern "system" {
    fn Shell_NotifyIconW(msg: u32, data: *const NotifyIconDataW) -> i32;
}

// ========================= Globals =========================

static HID: AtomicIsize = AtomicIsize::new(0); // cached device handle
static TRAY_HWND: AtomicIsize = AtomicIsize::new(0);

// KVM watcher state. STABLE=0 means "not synced yet": the first check after
// startup sends whatever profile matches the observed count (idempotent).
static KVM_STABLE: AtomicU32 = AtomicU32::new(0); // count the firmware was last told about
static KVM_PENDING: AtomicU32 = AtomicU32::new(0); // candidate count, 0 = none
static KVM_PENDING_AT: AtomicU32 = AtomicU32::new(0); // tick when the candidate first appeared

// ========================= Helpers =========================

fn wide(s: &str) -> Vec<u16> {
    s.encode_utf16().chain(std::iter::once(0)).collect()
}

fn fill_wide(dst: &mut [u16], s: &str) {
    let chars: Vec<u16> = s.encode_utf16().take(dst.len() - 1).collect();
    dst[..chars.len()].copy_from_slice(&chars);
    // rest is already zero-terminated
}

// ========================= HID device =========================

fn get_hid() -> Option<Handle> {
    let cached = HID.load(Ordering::Relaxed);
    if cached != 0 {
        return Some(cached);
    }
    match open_vendor_hid() {
        Some(h) => {
            HID.store(h, Ordering::Relaxed);
            Some(h)
        }
        None => None,
    }
}

fn hid_invalidate() {
    HID.store(0, Ordering::Relaxed);
}

/// Enumerate HID interfaces and open the DeskHop vendor collection (MI_02).
fn open_vendor_hid() -> Option<Handle> {
    unsafe {
        let mut guid = Guid::default();
        HidD_GetHidGuid(&mut guid);

        let info = SetupDiGetClassDevsW(&guid, std::ptr::null(), 0, DIGCF_PRESENT | DIGCF_DEVICEINTERFACE);
        if info == 0 || info == INVALID_HANDLE {
            return None;
        }

        let if_data_size = if std::mem::size_of::<usize>() == 8 { 32usize } else { 16usize };
        let mut if_data = vec![0u8; if_data_size];
        if_data[0..4].copy_from_slice(&(if_data_size as u32).to_ne_bytes());

        let mut found: Option<Handle> = None;
        for idx in 0..64u32 {
            if SetupDiEnumDeviceInterfaces(info, std::ptr::null(), &guid, idx, if_data.as_mut_ptr()) == 0 {
                break;
            }

            // Query required size, then fetch the device path
            let mut need: u32 = 0;
            SetupDiGetDeviceInterfaceDetailW(
                info, if_data.as_ptr(), std::ptr::null_mut(), 0, &mut need, std::ptr::null_mut(),
            );
            if need == 0 {
                continue;
            }
            let mut detail = vec![0u8; need as usize];
            let cb = if std::mem::size_of::<usize>() == 8 { 8u32 } else { 6u32 };
            detail[0..4].copy_from_slice(&cb.to_ne_bytes());
            if SetupDiGetDeviceInterfaceDetailW(
                info, if_data.as_ptr(), detail.as_mut_ptr(), need, std::ptr::null_mut(), std::ptr::null_mut(),
            ) == 0
            {
                continue;
            }

            // DevicePath starts at offset 4 (after cbSize), UTF-16
            let path_u16: Vec<u16> = detail[4..]
                .chunks_exact(2)
                .take_while(|c| c[0] != 0 || c[1] != 0)
                .map(|c| u16::from_ne_bytes([c[0], c[1]]))
                .collect();
            let path = String::from_utf16_lossy(&path_u16).to_lowercase();

            if path.contains(VID_PID) && path.contains(IFACE_SUFFIX) {
                let wpath = wide(&path);
                let h = CreateFileW(
                    wpath.as_ptr(),
                    GENERIC_READ | GENERIC_WRITE,
                    FILE_SHARE_READ | FILE_SHARE_WRITE,
                    std::ptr::null(),
                    OPEN_EXISTING,
                    0,
                    0,
                );
                if h != 0 && h != INVALID_HANDLE {
                    found = Some(h);
                }
                break;
            }
        }
        SetupDiDestroyDeviceInfoList(info);
        found
    }
}

// ========================= Protocol =========================

fn send_packet(h: Handle, t: u8, data: &[u8; 8]) -> bool {
    let mut pkt = [0u8; USB_PKT_LEN];
    pkt[0] = REPORT_ID;
    pkt[1] = 0xAA;
    pkt[2] = 0x55;
    pkt[3] = t;
    pkt[4..12].copy_from_slice(data);
    pkt[12] = data.iter().fold(0u8, |a, b| a ^ b);
    unsafe { HidD_SetOutputReport(h, pkt.as_ptr(), USB_PKT_LEN as u32) != 0 }
}

/// Push text to the other board. Returns (bytes sent, truncated).
fn push_text(h: Handle, text: &str) -> Result<(usize, bool), String> {
    let bytes = text.as_bytes();
    let total = bytes.len().min(MAX_TEXT);
    if total == 0 {
        return Err("Text is empty".into());
    }
    let truncated = bytes.len() > MAX_TEXT;

    let mut d = [0u8; 8];
    d[0] = (total & 0xFF) as u8;
    d[1] = ((total >> 8) & 0xFF) as u8;
    if !send_packet(h, TC_PUSH_BEGIN, &d) {
        return Err("USB send failed (BEGIN)".into());
    }

    let mut seq: u8 = 0;
    let mut sent = 0usize;
    while sent < total {
        let chunk = (total - sent).min(7);
        let mut d = [0u8; 8];
        d[0] = seq;
        d[1..1 + chunk].copy_from_slice(&bytes[sent..sent + chunk]);
        if !send_packet(h, TC_PUSH_DATA, &d) {
            return Err(format!("USB send failed at byte {sent}"));
        }
        sent += chunk;
        seq = seq.wrapping_add(1);
    }
    Ok((total, truncated))
}

/// Pull text from this machine's Pico. Ok(None) = nothing buffered.
fn pull_text(h: Handle) -> Result<Option<String>, String> {
    if !send_packet(h, TC_PULL_REQ, &[0u8; 8]) {
        return Err("USB send failed (PULL_REQ)".into());
    }

    let mut buf = vec![0u8; MAX_TEXT + 1];
    let mut got = 0usize;
    let mut rbuf = [0u8; USB_PKT_LEN];
    rbuf[0] = REPORT_ID; // GET_REPORT expects the report id in byte 0

    for _ in 0..1300 {
        if unsafe { HidD_GetInputReport(h, rbuf.as_mut_ptr(), USB_PKT_LEN as u32) } == 0 {
            return Err("USB read failed (stale handle?)".into());
        }
        if rbuf[1] != 0xAA || rbuf[2] != 0x55 {
            return Err("Bad packet framing".into());
        }
        let t = rbuf[3];
        if t == TC_PULL_EMPTY {
            break;
        }
        if t != TC_PULL_DATA {
            return Err(format!("Unexpected packet type {t}"));
        }
        let sum: u8 = rbuf[4..12].iter().fold(0u8, |a, b| a ^ b);
        if sum != rbuf[12] {
            return Err("Checksum mismatch".into());
        }
        let flags = rbuf[11];
        let eof = flags & 0x80 != 0;
        let chunk = if eof { (flags & 0x7F) as usize } else { 7 };
        if chunk == 0 {
            break;
        }
        if got + chunk > MAX_TEXT {
            return Err("Buffer overrun".into());
        }
        buf[got..got + chunk].copy_from_slice(&rbuf[4..4 + chunk]);
        got += chunk;
        if eof {
            break;
        }
    }

    if got == 0 {
        return Ok(None);
    }
    Ok(Some(String::from_utf8_lossy(&buf[..got]).into_owned()))
}

// ========================= Clipboard =========================

fn clipboard_get_text() -> Option<String> {
    unsafe {
        if OpenClipboard(0) == 0 {
            return None;
        }
        let h = GetClipboardData(CF_UNICODETEXT);
        if h == 0 {
            CloseClipboard();
            return None;
        }
        let p = GlobalLock(h);
        let mut result = None;
        if !p.is_null() {
            let mut chars = Vec::new();
            let mut i = 0usize;
            loop {
                let c = *(p.add(i * 2) as *const u16);
                if c == 0 || i > 1_000_000 {
                    break;
                }
                chars.push(c);
                i += 1;
            }
            result = Some(String::from_utf16_lossy(&chars));
            GlobalUnlock(h);
        }
        CloseClipboard();
        result
    }
}

fn clipboard_set_text(s: &str) -> bool {
    unsafe {
        if OpenClipboard(0) == 0 {
            return false;
        }
        EmptyClipboard();
        let wide: Vec<u16> = s.encode_utf16().collect();
        let bytes = (wide.len() + 1) * 2;
        let h = GlobalAlloc(GMEM_MOVEABLE, bytes);
        let mut ok = false;
        if h != 0 {
            let p = GlobalLock(h);
            if !p.is_null() {
                std::ptr::copy_nonoverlapping(wide.as_ptr(), p as *mut u16, wide.len());
                *(p.add(wide.len() * 2) as *mut u16) = 0;
                GlobalUnlock(h);
                // Ownership of h transfers to the clipboard on success.
                if SetClipboardData(CF_UNICODETEXT, h) != 0 {
                    ok = true;
                } else {
                    GlobalFree(h);
                }
            } else {
                GlobalFree(h);
            }
        }
        CloseClipboard();
        ok
    }
}

// ========================= Actions =========================

fn do_push() {
    let Some(h) = get_hid() else {
        return;
    };
    // Push the clipboard as-is: the user copies the text themselves, so this
    // is safe everywhere (an injected ^c would SIGINT remote shells over SSH).
    let Some(text) = clipboard_get_text().filter(|t| !t.is_empty()) else {
        return;
    };
    if let Err(_) = push_text(h, &text) {
        hid_invalidate();
    }
}

fn do_pull() {
    let Some(h) = get_hid() else {
        return;
    };

    match pull_text(h) {
        Err(_) => {
            hid_invalidate();
        }
        Ok(None) => {}
        Ok(Some(text)) => {
            // Clipboard only: pasting is left to the user, since terminals
            // disagree (^v vs Ctrl+Shift+V vs right-click) and mstsc may not
            // even deliver injected keys.
            clipboard_set_text(&text);
        }
    }
}

// ========================= KVM watcher =========================

/// Monitor count -> layout profile. 2 = side display present = HOST (P1),
/// 1 = only the real display = LAPTOP (P2). Verified 2026-10-13: the KVM
/// button drops/raises the phantom display on the host, flipping SM_CMONITORS.
fn count_to_profile(count: u32) -> Option<u8> {
    match count {
        2 => Some(0), // LAYOUT_PROFILE_HOST
        1 => Some(1), // LAYOUT_PROFILE_LAPTOP
        _ => None,    // unusual topology, stay out of the way
    }
}

/// Evaluate the monitor count against the debounce state machine. Called on
/// WM_DISPLAYCHANGE (event), the 1 s debounce timer and the 5 s reconcile
/// timer. Sends the profile only once a candidate count has held steady for
/// KVM_DEBOUNCE_MS, so source-switch transients (VCP glitches, HPD churn)
/// are ignored. Absolute values keep manual hotkey toggles resolvable.
///
/// Change-driven by design: the tool runs on BOTH machines, but only the
/// host ever sees the count flip (the laptop's phantom is permanent), so
/// "send on change" makes the laptop side a silent no-op. Sending on a
/// startup reading instead would let the laptop force OUTPUT_A ownership
/// onto its own board - wrong. Startup therefore only samples the baseline.
fn kvm_check(hwnd: Handle) {
    let count = unsafe { GetSystemMetrics(SM_CMONITORS) } as u32;
    let Some(profile) = count_to_profile(count) else {
        return;
    };

    // Baseline sampling: observe only until the first actual change.
    if KVM_STABLE
        .compare_exchange(0, count, Ordering::Relaxed, Ordering::Relaxed)
        .is_ok()
    {
        return;
    }

    if count == KVM_STABLE.load(Ordering::Relaxed) {
        // Spurious event or already in sync - cancel any pending candidate.
        KVM_PENDING.store(0, Ordering::Relaxed);
        unsafe { KillTimer(hwnd, KVM_TIMER_DEBOUNCE) };
        return;
    }

    let now = unsafe { GetTickCount() };

    if KVM_PENDING.load(Ordering::Relaxed) != count {
        // New candidate: (re)start the debounce window.
        KVM_PENDING.store(count, Ordering::Relaxed);
        KVM_PENDING_AT.store(now, Ordering::Relaxed);
        unsafe { SetTimer(hwnd, KVM_TIMER_DEBOUNCE, KVM_DEBOUNCE_MS, 0) };
        return;
    }

    // Same candidate as before - act if it has held long enough.
    let since = now.wrapping_sub(KVM_PENDING_AT.load(Ordering::Relaxed));
    if since < KVM_DEBOUNCE_MS {
        return; // debounce timer will re-check
    }

    let mut d = [0u8; 8];
    d[0] = profile;
    if let Some(h) = get_hid() {
        if send_packet(h, TC_LAYOUT_SELECT, &d) {
            KVM_STABLE.store(count, Ordering::Relaxed);
        } else {
            hid_invalidate();
        }
    }
    KVM_PENDING.store(0, Ordering::Relaxed);
    unsafe { KillTimer(hwnd, KVM_TIMER_DEBOUNCE) };
}

// ========================= Tray =========================

fn add_tray(hwnd: Handle) {
    unsafe {
        let mut n: NotifyIconDataW = std::mem::zeroed();
        n.cb_size = std::mem::size_of::<NotifyIconDataW>() as u32;
        n.hwnd = hwnd;
        n.uid = 1;
        n.uflags = NIF_MESSAGE | NIF_ICON | NIF_TIP;
        n.callback_msg = TRAY_CALLBACK;
        let instance = GetModuleHandleW(std::ptr::null());
        let icon = LoadIconW(instance, 1usize as *const u16); // resource ID 1 (icon.rc)
        n.hicon = if icon != 0 {
            icon
        } else {
            LoadIconW(0, 32512usize as *const u16) // IDI_APPLICATION fallback
        };
        fill_wide(&mut n.sz_tip, "DeskHop text channel");
        Shell_NotifyIconW(NIM_ADD, &n);
    }
}

fn remove_tray(hwnd: Handle) {
    unsafe {
        let mut n: NotifyIconDataW = std::mem::zeroed();
        n.cb_size = std::mem::size_of::<NotifyIconDataW>() as u32;
        n.hwnd = hwnd;
        n.uid = 1;
        Shell_NotifyIconW(NIM_DELETE, &n);
    }
}

extern "system" fn wnd_proc(hwnd: Handle, msg: u32, wp: usize, lp: isize) -> isize {
    unsafe {
        match msg {
            WM_HOTKEY => match wp {
                1 => do_push(),
                2 => do_pull(),
                3 => PostQuitMessage(0),
                _ => {}
            },

            TRAY_CALLBACK => {
                if lp as u32 == WM_RBUTTONUP {
                    let menu = CreatePopupMenu();
                    AppendMenuW(menu, MF_STRING, 1, wide("Exit").as_ptr());
                    let mut pt = Point::default();
                    GetCursorPos(&mut pt);
                    SetForegroundWindow(hwnd);
                    TrackPopupMenu(menu, TPM_RIGHTBUTTON, pt.x, pt.y, 0, hwnd, std::ptr::null());
                    DestroyMenu(menu);
                }
            }

            WM_COMMAND => {
                if wp & 0xFFFF == 1 {
                    PostQuitMessage(0);
                }
            }

            WM_DISPLAYCHANGE => {
                // Display topology changed (KVM button press on the host).
                kvm_check(hwnd);
            }

            WM_TIMER => {
                if wp == KVM_TIMER_DEBOUNCE || wp == KVM_TIMER_RECONCILE {
                    kvm_check(hwnd);
                }
            }

            WM_DESTROY => {
                remove_tray(hwnd);
                PostQuitMessage(0);
            }

            _ => return DefWindowProcW(hwnd, msg, wp, lp),
        }
    }
    0
}

// ========================= Main =========================

fn main() {
    unsafe {
        let instance = GetModuleHandleW(std::ptr::null());
        let class_name = wide("DeskHopTextChannel");

        let wc = WndClassW {
            style: 0,
            wnd_proc: Some(wnd_proc),
            cls_extra: 0,
            wnd_extra: 0,
            instance,
            icon: 0,
            cursor: 0,
            background: 0,
            menu_name: std::ptr::null(),
            class_name: class_name.as_ptr(),
        };
        RegisterClassW(&wc);

        let hwnd = CreateWindowExW(
            0, class_name.as_ptr(), wide("deskhop-text-channel").as_ptr(),
            0, // hidden (no WS_VISIBLE)
            0, 0, 0, 0, 0, 0, instance, std::ptr::null(),
        );
        if hwnd == 0 {
            return;
        }
        TRAY_HWND.store(hwnd, Ordering::Relaxed);
        add_tray(hwnd);

        // Ctrl+Alt+U push clipboard / Ctrl+Alt+D pull to clipboard / Ctrl+Alt+0 exit
        let mods = MOD_CONTROL | MOD_ALT;
        RegisterHotKey(hwnd, 1, mods, 0x55); // U
        RegisterHotKey(hwnd, 2, mods, 0x44); // D
        RegisterHotKey(hwnd, 3, mods, 0x30); // 0

        // KVM watcher: periodic reconcile catches what the event path misses
        // (events lost before window creation, resume from sleep). Startup
        // only samples the baseline; the first actual count change triggers
        // the first send (change-driven - see kvm_check).
        SetTimer(hwnd, KVM_TIMER_RECONCILE, KVM_RECONCILE_MS, 0);

        let mut msg: Msg = std::mem::zeroed();
        while GetMessageW(&mut msg, 0, 0, 0) > 0 {
            TranslateMessage(&msg);
            DispatchMessageW(&msg);
        }
    }
}
