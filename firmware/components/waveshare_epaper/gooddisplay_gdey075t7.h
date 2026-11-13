// Modified for WildStak from parkghost/esphome-epaper (c6ac50c, Apache-2.0); see README.md.
#pragma once

#include "waveshare_epaper.h"

namespace esphome {
namespace waveshare_epaper {

class GDEY075T7 : public WaveshareEPaper {
 public:
  static const char *const TAG;

  static const uint16_t WIDTH = 800;

  static const uint16_t HEIGHT = 480;

  // Datasheet full update time is 3.5 s typ. @ 25 C and slows down markedly at
  // low temperature, so keep a generous timeout to avoid aborting mid-refresh.
  static const uint16_t IDLE_TIMEOUT = 10000;

  void setup() override;

  void initialize() override;

  void dump_config() override;

  void display() override;

  void deep_sleep() override;

  void set_full_update_every(uint32_t full_update_every);

  void full_refresh();

  // Deep-sleep persistence hooks. An ESP32 deep sleep wipes RAM, so both the
  // refresh counter and the partial-refresh base frame (old_data_) are lost on
  // every wake, which would make the first refresh after each wake a full one.
  // The firmware keeps the counter in RTC memory and restores it with
  // set_update_count(), then calls capture_previous_frame() while its display
  // lambda draws whatever the panel is currently showing.
  uint32_t get_update_count() const { return this->at_update_; }
  void set_update_count(uint32_t count) {
    this->at_update_ = this->full_update_every_ == 0 ? count : count % this->full_update_every_;
  }
  // Render the display lambda into old_data_ without touching the panel.
  void capture_previous_frame();
  // Direct framebuffer access for the flash card cache: a cached card is the
  // panel framebuffer itself, so a rotation wake reads it straight into these
  // instead of rendering. Both are get_frame_size() bytes.
  uint8_t *frame_buffer() { return this->buffer_; }
  uint8_t *previous_frame_buffer() { return this->old_data_; }
  size_t get_frame_size() { return this->get_buffer_length_(); }
  // Call after filling previous_frame_buffer() with what the panel shows.
  void mark_previous_frame_valid() { this->old_data_valid_ = this->old_data_ != nullptr; }
  // Run the display lambda into frame_buffer() without touching the panel.
  void render_only();
  // Refresh the panel from frame_buffer() as-is (no display lambda).
  void show_loaded_frame();
  // Whether the most recent display() call used the full-refresh waveform.
  bool last_refresh_was_full() const { return this->last_full_; }

  // Per-phase durations (microseconds) of the most recent capture/update, for
  // tuning the rotation-wake cost. Only the partial-refresh path fills the
  // init/SPI/refresh fields.
  struct Timing {
    uint32_t capture_us;        // capture_previous_frame(): render + copy
    uint32_t render_us;         // update(): display lambda into buffer_
    uint32_t reset_us;          // reset_() pulse
    uint32_t power_on_us;       // POWER ON: fixed delay + busy wait
    uint32_t power_on_wait_us;  //   of which: busy wait after the fixed delay
    uint32_t old_spi_us;        // write previous frame (0x10)
    uint32_t new_spi_us;        // write new frame (0x13)
    uint32_t refresh_us;        // 0x12 + busy wait (the waveform itself)
    uint32_t power_off_us;      // 0x02 + busy wait in deep_sleep()
    uint32_t light_sleep_us;    // time spent in ESP32 light sleep during display()
    uint32_t light_sleep_count;
  };
  const Timing &get_timing() const { return this->timing_; }

  // When true, waits on the panel's busy signal put the ESP32 into light sleep
  // (woken by BUSY going idle, with a timer backstop) instead of busy-polling
  // at full clock. The panel's RESET/DC/CS outputs are pad-held while asleep so
  // they can't glitch mid-refresh. Only enable this while Wi-Fi is off: light
  // sleep stalls the radio.
  void set_light_sleep_waits(bool enable) { this->light_sleep_waits_ = enable; }

  void update() override;

 protected:
  Timing timing_{};
  bool light_sleep_waits_{false};

  // Busy wait that light-sleeps when light_sleep_waits_ is set.
  bool wait_idle_();
  // One light sleep of at most max_ms, optionally ending early when BUSY goes idle.
  void light_sleep_(uint32_t max_ms, bool wake_on_idle);
  void hold_pins_(bool hold);

  int get_width_internal() override;

  int get_height_internal() override;

  uint32_t idle_timeout_() override;

  bool is_busy_pin_inverted_() override { return true; }

  void reset_();

  // Full-refresh initialization: full power/booster setup, drives the panel with
  // the flashing full-refresh waveform for best de-ghosting.
  void init_full_();

  // Fast-refresh initialization (mirrors the vendor EPD_Init_Fast, E5=0x5A):
  // whole-screen ~1.5 s waveform. Cleaner than partial but flashes once.
  // Retained for reference; not used by display() currently.
  void init_fast_();

  // Lightweight partial-refresh initialization (mirrors the vendor
  // EPD_Init_Part), avoids re-running the heavy power/booster setup.
  void init_partial_();

  // Write the whole framebuffer in normal mode (old=0x00, new=buffer_ as-is).
  // Used by the full and (retained) fast paths; the waveform comes from init.
  bool write_frame_();

  // Differential partial refresh (fast, no flicker, accumulates ghosting).
  bool write_partial_();

  // Trigger a display refresh (0x12) and wait for the panel to become idle.
  bool refresh_();

  // Previous frame, used as the differential base for partial updates.
  // Allocated at setup to avoid a large fixed member (48 KB for this panel).
  uint8_t *old_data_{nullptr};

  // False until old_data_ holds the frame actually on the panel (set by a
  // refresh or by capture_previous_frame()). A partial refresh against a stale
  // or uninitialized base leaves artifacts, so display() forces a full refresh
  // instead.
  bool old_data_valid_{false};

  // True after the 0x07 deep-sleep command; the controller then ignores
  // everything until a hardware reset. Lets deep_sleep() skip the redundant
  // second call from on_safe_shutdown() when the ESP32 itself goes to sleep.
  bool panel_asleep_{false};

  bool last_full_{false};

  uint32_t at_update_{0}, full_update_every_{30};
};

}  // namespace waveshare_epaper
}  // namespace esphome
