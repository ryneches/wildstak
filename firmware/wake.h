#pragma once
// v0.3 control logic (NOTES.md, "Firmware redesign (v0.3, proposed)").
//
// Two modes:
//   Carousel: one wake is one pass. Decide from the clock which card should
//     be on the panel, draw it (radio off, light-sleep refresh), sync if due,
//     then deep sleep until the next deadline. Cards only.
//   Service: entered by the green button or any power-on/reflash. Wi-Fi up,
//     immediate sync, a live status screen with a countdown bar, reachable on
//     the network until the countdown ends; then back to the carousel.
//
// All carousel scheduling is a pure function of the RTC time, the settings
// and the flash cache; per-wake state is `state`, in RTC memory. YAML owns the
// network steps (Wi-Fi, HTTP, SNTP) and calls into this file for the rest.

#include <cstdint>
#include <cstdlib>
#include <ctime>
#include <string>
#include <type_traits>
#include <vector>

#include "esp_attr.h"
#include "esp_sleep.h"

#include "esphome/components/deep_sleep/deep_sleep_component.h"
#include "esphome/components/waveshare_epaper/gooddisplay_gdey075t7.h"
#include "esphome/core/log.h"
#include "esphome/core/time.h"

#include "card_cache.h"

namespace wake {

using Display = esphome::waveshare_epaper::GDEY075T7;
using DeepSleep = esphome::deep_sleep::DeepSleepComponent;

static const char *const TAG = "wake";

// ---- Settings. Defaults come from YAML substitutions at boot. Home
// Assistant changes them through the device's own MQTT controls (number /
// select / switch entities with command_retain, so each setting's latest
// value waits at the broker until the device connects); applied values are
// kept in RTC memory (state.ha_settings), so they survive deep sleep. After a
// power loss the defaults apply until the first sync, which on a cold boot is
// immediate (service mode).
// No default member initializers: State embeds a Settings, and they would give
// State a constructor that runs on every boot, deep-sleep wakes included,
// overwriting the RTC copy with the defaults (v0.3.0 bug). ----
struct Settings {
  uint32_t rotate_s;
  uint32_t sync_period_s;       // divides 3600
  uint32_t sync_offset_s;
  uint32_t max_sleep_s;         // heartbeat cap
  uint32_t ntp_every_s;         // wait for SNTP at most this often
  uint32_t service_s;           // service mode countdown
  uint32_t full_refresh_every;  // refreshes between de-ghosting full refreshes; 0 = never
  bool deep_sleep;              // false: stay awake, radio on (bench / USB power)
};
static Settings settings = {
    .rotate_s = 60,
    .sync_period_s = 3600,
    .sync_offset_s = 300,
    .max_sleep_s = 3600,
    .ntp_every_s = 86400,
    .service_s = 120,
    .full_refresh_every = 60,
    .deep_sleep = true,
};

enum class Shown : uint8_t { NONE = 0, CARD, MESSAGE, SERVICE };
enum SyncResult : uint8_t { SYNC_NEVER = 0, SYNC_OK, SYNC_NO_WIFI, SYNC_MANIFEST_FAILED, SYNC_CARD_FAILED };

// RTC memory: survives deep sleep, cleared by power loss / reset.
struct State {
  uint32_t magic;
  int32_t manual_offset;  // white buttons shift the carousel position
  uint32_t at_update;     // display driver refresh counter
  Shown shown;            // what is on the panel...
  int8_t shown_slot;      // ...and which card
  int32_t n_cards;
  uint32_t last_sync_attempt;
  uint32_t last_sync_ok;
  uint32_t last_ntp;
  uint32_t cards_generated_at;
  uint32_t last_sync_ms;
  uint8_t last_sync_result;
  float battery_v;
  float temp_c;
  float humidity;
  int8_t battery_pct;  // -1 = never measured
  int8_t rssi;
  uint32_t wakes;
  uint32_t last_active_ms;
  uint32_t next_deadline;
  Settings ha_settings;  // last settings received from Home Assistant
  uint8_t has_ha_settings;
};
static constexpr uint32_t MAGIC = 0x57414B35;  // "WAK5"
static_assert(std::is_trivially_default_constructible_v<State>,
              "RTC state must not have a constructor: it would run on every deep-sleep wake");
static RTC_DATA_ATTR State state;

// Per boot (RAM).
struct Boot {
  bool cold = false;
  bool button_wake = false;
  bool pass_started = false;
  bool sync_first = false;
  bool sync_requested = false;
  bool redraw_requested = false;
  bool ota_in_progress = false;
  bool radio_on = false;
  bool base_in_ram = false;      // driver's previous frame = what the panel shows
  bool shown_rewritten = false;  // a sync replaced the card on the panel
  bool full_pending = false;     // de-ghosting refresh deferred out of service mode
  uint32_t awake_until_ms = 0;
  int settings_received = 0;     // setting commands applied this boot
  // Service mode.
  bool service_requested = false;
  bool service_active = false;
  bool service_exit = false;
  bool service_syncing = false;
  bool service_dirty = false;    // status changed: redraw now, not at the next bar step
  uint32_t service_until_ms = 0;
  int service_step = -1;         // bar step last drawn
};
static Boot boot;

static constexpr uint32_t INTERACTION_AWAKE_MS = 30000;
static constexpr uint32_t BUTTON_IGNORE_MS = 1000;  // the waking press also reports on_press
static constexpr uint32_t BAR_STEP_MS = 5000;       // countdown bar redraw interval
static constexpr uint64_t BUTTON_GREEN = 1ULL << 3;  // GPIO3: service mode
static constexpr uint64_t BUTTON_NEXT = 1ULL << 4;   // GPIO4, right white
static constexpr uint64_t BUTTON_PREV = 1ULL << 5;   // GPIO5, left white

// ---- Time and schedule ----
inline uint32_t now() { return (uint32_t) ::time(nullptr); }
inline bool time_valid() { return now() > 1700000000UL; }

inline uint32_t sync_boundary(uint32_t t) {
  const uint32_t p = settings.sync_period_s, o = settings.sync_offset_s % p;
  return t < o ? 0 : ((t - o) / p) * p + o;
}
inline bool sync_due(uint32_t t) {
  return !time_valid() || state.n_cards == 0 || state.last_sync_attempt < sync_boundary(t);
}

// The wake timer runs off an RC oscillator and can fire a little early; treat
// "a moment before the deadline" as the deadline.
inline uint32_t effective_now() {
  const uint32_t t = now();
  return (state.next_deadline != 0 && t < state.next_deadline && state.next_deadline - t <= 2) ? state.next_deadline
                                                                                                : t;
}

inline int carousel_index(uint32_t t) {
  const int n = state.n_cards;
  if (n <= 0)
    return -1;
  const int64_t slot = (int64_t) (t / settings.rotate_s) + state.manual_offset;
  return (int) (((slot % n) + n) % n);
}

inline uint32_t next_deadline(uint32_t t) {
  uint32_t d = (t / settings.rotate_s + 1) * settings.rotate_s;
  const uint32_t s = sync_boundary(t) + settings.sync_period_s;
  if (s < d)
    d = s;
  if (t + settings.max_sleep_s < d)
    d = t + settings.max_sleep_s;
  return d;
}

// ---- Boot and buttons ----
inline void on_boot() {
  const auto cause = esp_sleep_get_wakeup_cause();
  const bool timer_wake = cause == ESP_SLEEP_WAKEUP_TIMER;
  const bool ext1_wake = cause == ESP_SLEEP_WAKEUP_EXT1;
  boot.cold = state.magic != MAGIC || (!timer_wake && !ext1_wake);
  if (state.magic != MAGIC) {
    state = State{};
    state.magic = MAGIC;
    state.battery_pct = -1;
    state.shown_slot = -1;
    // RTC memory was lost, but the flash cache wasn't: count its cards
    // (slots are filled from 0 in manifest order), so the carousel works even
    // if the first sync after a power loss fails.
    card_cache::Header h;
    while (state.n_cards < card_cache::slot_count() && card_cache::read_header(state.n_cards, h))
      state.n_cards++;
  }
  state.wakes++;
  if (state.has_ha_settings)
    settings = state.ha_settings;
  if (boot.cold) {
    state.shown = Shown::NONE;  // panel contents unknown after a reset
    state.next_deadline = 0;
    boot.service_requested = true;  // every power-on / reflash starts reachable
  }
  if (ext1_wake) {
    const uint64_t pins = esp_sleep_get_ext1_wakeup_status();
    boot.button_wake = true;
    if (pins & BUTTON_GREEN) {
      boot.service_requested = true;
    } else {
      boot.awake_until_ms = INTERACTION_AWAKE_MS;
      if (pins & BUTTON_NEXT)
        state.manual_offset++;
      else if (pins & BUTTON_PREV)
        state.manual_offset--;
    }
  }
  ESP_LOGI(TAG, "boot: cold=%d button=%d service=%d time_valid=%d cards=%d", (int) boot.cold, (int) boot.button_wake,
           (int) boot.service_requested, (int) time_valid(), (int) state.n_cards);
}

inline void on_button(uint64_t which) {
  if (esphome::millis() < BUTTON_IGNORE_MS)
    return;
  if (boot.service_active) {
    if (which == BUTTON_GREEN) {  // more time
      boot.service_until_ms = esphome::millis() + settings.service_s * 1000;
      boot.service_dirty = true;
    } else {  // back to the cards
      boot.service_exit = true;
    }
    return;
  }
  if (which == BUTTON_GREEN) {
    boot.service_requested = true;
    return;
  }
  boot.awake_until_ms = esphome::millis() + INTERACTION_AWAKE_MS;
  state.manual_offset += which == BUTTON_NEXT ? 1 : -1;
  boot.redraw_requested = true;
}

// ---- Settings from Home Assistant (the device's MQTT controls) ----
// Each setter validates, applies and persists one setting; out-of-range values
// are ignored (the control then reports the unchanged value back to HA).
inline void store_setting(const char *what) {
  state.ha_settings = settings;
  state.has_ha_settings = 1;
  boot.settings_received++;
  ESP_LOGI(TAG, "setting %s: rotate %u s, sync every %u min +%u, service %u s, full refresh every %u, deep sleep %d",
           what, (unsigned) settings.rotate_s, (unsigned) (settings.sync_period_s / 60),
           (unsigned) (settings.sync_offset_s / 60), (unsigned) settings.service_s,
           (unsigned) settings.full_refresh_every, (int) settings.deep_sleep);
}
inline bool in_range(const char *what, float v, float lo, float hi) {
  if (v >= lo && v <= hi)
    return true;
  ESP_LOGW(TAG, "setting %s=%.0f out of range [%.0f, %.0f], ignored", what, v, lo, hi);
  return false;
}
inline void set_rotate_s(float v) {
  if (in_range("rotate_s", v, 10, 86400))
    settings.rotate_s = (uint32_t) v;
  store_setting("rotate_s");
}
inline void set_sync_period_min(int m) {
  if (in_range("sync_period_min", m, 1, 60) && 60 % m == 0) {
    settings.sync_period_s = m * 60;
    settings.sync_offset_s %= settings.sync_period_s;  // offset must stay inside the period
  }
  store_setting("sync_period_min");
}
inline void set_sync_offset_min(float v) {
  if (in_range("sync_offset_min", v, 0, 59))
    settings.sync_offset_s = ((uint32_t) v % (settings.sync_period_s / 60)) * 60;
  store_setting("sync_offset_min");
}
inline void set_service_s(float v) {
  if (in_range("service_s", v, 30, 3600))
    settings.service_s = (uint32_t) v;
  store_setting("service_s");
}
inline void set_full_refresh_every(float v) {
  if (in_range("full_refresh_every", v, 0, 1000))
    settings.full_refresh_every = (uint32_t) v;
  store_setting("full_refresh_every");
}
inline void set_deep_sleep(bool on) {
  settings.deep_sleep = on;
  store_setting("deep_sleep");
}

// ---- Rendering hooks for the YAML display lambda ----
enum class Mode : uint8_t { CARD_IMAGE, MESSAGE, SERVICE };
struct RenderRequest {
  Mode mode = Mode::MESSAGE;
  const char *message = "";
};
static RenderRequest render;

// Service screen countdown, for the display lambda.
inline uint32_t service_remaining_ms() {
  const uint32_t ms = esphome::millis();
  return boot.service_until_ms > ms ? boot.service_until_ms - ms : 0;
}

// ---- Refreshing ----
// Make the driver's previous frame match the panel. Within one awake session
// the driver still has it in RAM; after a deep sleep only a card can be
// rebuilt (from flash). Anything else means the driver does a full refresh.
inline void ensure_base(Display *d) {
  if (boot.base_in_ram)
    return;
  if (state.shown == Shown::CARD && card_cache::read_frame(state.shown_slot, d->previous_frame_buffer()))
    d->mark_previous_frame_valid();
}

// Refresh from frame_buffer(). In service mode a scheduled de-ghosting full
// refresh is deferred to the exit, so it never flashes mid-countdown.
inline void refresh(Display *d) {
  d->set_update_count(state.at_update);
  if (boot.service_active && d->get_update_count() == 0 && settings.full_refresh_every != 0) {
    d->set_update_count(1);
    boot.full_pending = true;
  }
  d->set_light_sleep_waits(!boot.radio_on);
  d->show_loaded_frame();
  if (d->last_refresh_was_full())
    boot.full_pending = false;  // it happened anyway (e.g. no valid previous frame)
  state.at_update = d->get_update_count();
  boot.base_in_ram = !d->status_has_warning();
  if (!boot.base_in_ram)
    state.shown = Shown::NONE;  // refresh failed: contents unknown
}

// Load carousel item `index` into frame_buffer(); updates state.shown.
inline void load_carousel(Display *d, int index) {
  if (index >= 0 && card_cache::read_frame(index, d->frame_buffer())) {
    state.shown = Shown::CARD;
    state.shown_slot = (int8_t) index;
    return;
  }
  render.mode = Mode::MESSAGE;
  render.message = !time_valid() ? "Waiting for the clock" : "No cards yet";
  d->render_only();
  state.shown = Shown::MESSAGE;
  state.shown_slot = -1;
}

// Draw the card the clock calls for. Skips the refresh if it's already up.
inline void draw_carousel(Display *d, bool force) {
  const int index = carousel_index(effective_now());
  if (!force && state.shown == Shown::CARD && index == state.shown_slot) {
    ESP_LOGD(TAG, "card %d already shown", index);
    return;
  }
  ensure_base(d);
  load_carousel(d, index);
  refresh(d);
  ESP_LOGI(TAG, "card %d/%d (%s refresh)", index, (int) state.n_cards,
           d->last_refresh_was_full() ? "full" : "partial");
}

// After a sync replaced the card on the panel: show the new version (the
// previous frame is still in RAM from this wake's refresh).
inline void redraw_rewritten(Display *d) {
  if (!boot.shown_rewritten || state.shown != Shown::CARD)
    return;
  boot.shown_rewritten = false;
  if (!card_cache::read_frame(state.shown_slot, d->frame_buffer()))
    return;
  refresh(d);
  ESP_LOGI(TAG, "redrew card %d after it changed", (int) state.shown_slot);
}

// ---- Service mode ----
inline void service_begin() {
  boot.service_requested = false;
  boot.service_active = true;
  boot.service_exit = false;
  boot.service_dirty = true;
  boot.service_step = -1;
  boot.service_until_ms = esphome::millis() + settings.service_s * 1000;
  ESP_LOGI(TAG, "service mode for %u s", (unsigned) settings.service_s);
}

// Redraw the service screen when its status changed or the bar moved a step.
inline void service_update(Display *d) {
  const int step = (int) (service_remaining_ms() / BAR_STEP_MS);
  if (!boot.service_dirty && step == boot.service_step)
    return;
  boot.service_dirty = false;
  boot.service_step = step;
  ensure_base(d);
  render.mode = Mode::SERVICE;
  d->render_only();
  refresh(d);
  state.shown = Shown::SERVICE;
}

inline bool service_done() {
  return boot.service_exit ||
         (service_remaining_ms() == 0 && !boot.ota_in_progress && !boot.service_syncing);
}

// Back to the carousel: the service screen is the previous frame (in RAM), so
// this is a partial refresh, unless a de-ghosting refresh was deferred.
inline void service_end(Display *d) {
  boot.service_active = false;
  boot.service_exit = false;
  if (boot.full_pending) {
    state.at_update = 0;
    boot.full_pending = false;
  }
  ESP_LOGI(TAG, "service mode over");
  draw_carousel(d, true);
}

// ---- Manifest and sync bookkeeping (RAM) ----
static std::vector<std::string> urls;
static std::vector<uint64_t> hashes;
static uint32_t manifest_generated_at = 0;

// Manifest (see the Makefile): line 1 = ISO8601 UTC generated-at, then
// "<16 hex hash> <url>" per card, in rotation order.
inline bool parse_manifest(const std::string &body) {
  urls.clear();
  hashes.clear();
  manifest_generated_at = 0;
  size_t start = 0;
  int line_no = 0;
  while (start < body.size()) {
    const size_t nl = body.find('\n', start);
    std::string line = body.substr(start, nl == std::string::npos ? std::string::npos : nl - start);
    start = nl == std::string::npos ? body.size() : nl + 1;
    if (!line.empty() && line.back() == '\r')
      line.pop_back();
    if (line.empty())
      continue;
    if (line_no++ == 0) {
      std::string p = line;  // "2026-09-27T18:00:25Z"
      if (p.size() > 10 && p[10] == 'T')
        p[10] = ' ';
      if (!p.empty() && p.back() == 'Z')
        p.pop_back();
      esphome::ESPTime t{};
      if (esphome::ESPTime::strptime(p, t)) {
        t.day_of_week = 1;  // strptime leaves these unset; recalc gates on them
        t.day_of_year = 1;
        t.recalc_timestamp_utc(false);
        manifest_generated_at = (uint32_t) t.timestamp;
      }
      continue;
    }
    const size_t sp = line.find(' ');
    if (sp == std::string::npos)
      continue;
    hashes.push_back(strtoull(line.substr(0, sp).c_str(), nullptr, 16));
    urls.push_back(line.substr(sp + 1));
  }
  const int max_cards = card_cache::slot_count();
  if ((int) urls.size() > max_cards) {
    urls.resize(max_cards);
    hashes.resize(max_cards);
  }
  return !urls.empty();
}

inline bool card_needs_download(int i) {
  card_cache::Header h;
  return !(card_cache::read_header(i, h) && h.src_hash == hashes[i]);
}

// Store the freshly rendered card i (in frame_buffer()).
inline bool store_card(Display *d, int i) {
  const auto r = card_cache::write_frame(i, d->frame_buffer(), hashes[i]);
  if (r == card_cache::WRITE_DONE && state.shown == Shown::CARD && state.shown_slot == i)
    boot.shown_rewritten = true;
  return r != card_cache::WRITE_FAILED;
}

inline void finish_manifest() {
  state.n_cards = (int32_t) urls.size();
  state.cards_generated_at = manifest_generated_at;
}

// Keep the radio after a sync? Only while something keeps the device awake.
inline bool keep_radio() { return boot.service_active || !settings.deep_sleep; }

// ---- Passes and the sleep gate ----
inline void pass_begin() {
  boot.pass_started = true;
  boot.sync_first = !time_valid() || state.n_cards == 0;
}
inline bool pass_needs_sync() { return boot.sync_requested || sync_due(effective_now()); }
inline bool pass_wanted() {
  return boot.service_requested || boot.redraw_requested || boot.sync_requested ||
         (state.next_deadline != 0 && now() >= state.next_deadline);
}
inline void pass_end() {
  boot.redraw_requested = false;
  state.next_deadline = next_deadline(now());
}

inline bool may_sleep() {
  return settings.deep_sleep && !boot.service_active && !boot.service_requested && !boot.ota_in_progress &&
         esphome::millis() >= boot.awake_until_ms && !boot.sync_requested && !boot.redraw_requested;
}

// End of every pass, and the tick. Doesn't return if the device sleeps.
inline void try_sleep(DeepSleep *ds, bool any_button_down) {
  if (!may_sleep() || any_button_down)
    return;
  const uint32_t t = now();
  if (state.next_deadline <= t)
    state.next_deadline = next_deadline(t);
  const uint32_t secs = state.next_deadline > t ? state.next_deadline - t : 1;
  state.last_active_ms = esphome::millis();
  ESP_LOGI(TAG, "sleeping %u s", (unsigned) secs);
  ds->set_sleep_duration(secs * 1000);
  ds->begin_sleep(true);
}

}  // namespace wake
