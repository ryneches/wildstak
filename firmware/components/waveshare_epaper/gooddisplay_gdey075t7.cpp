// Modified for WildStak from parkghost/esphome-epaper (c6ac50c, Apache-2.0); see README.md.
#include "gooddisplay_gdey075t7.h"

#include <cstdint>

#include "esphome/core/helpers.h"
#include "esphome/core/log.h"

#include "driver/gpio.h"
#include "esp_sleep.h"

namespace esphome {
namespace waveshare_epaper {

const char *const GDEY075T7::TAG = "gdey075t7";

int GDEY075T7::get_width_internal() { return WIDTH; }

int GDEY075T7::get_height_internal() { return HEIGHT; }

uint32_t GDEY075T7::idle_timeout_() { return IDLE_TIMEOUT; }

void GDEY075T7::set_full_update_every(uint32_t full_update_every) {
  this->full_update_every_ = full_update_every;
}

void GDEY075T7::full_refresh() {
  this->at_update_ = 0;
  this->update();
}

void GDEY075T7::update() {
  const uint32_t t = micros();
  this->do_update_();
  this->timing_.render_us = micros() - t;
  this->display();
}

void GDEY075T7::render_only() {
  const uint32_t t = micros();
  this->do_update_();
  this->timing_.render_us = micros() - t;
}

void GDEY075T7::show_loaded_frame() {
  this->timing_.render_us = 0;
  this->display();
}

void GDEY075T7::capture_previous_frame() {
  if (this->old_data_ == nullptr)
    return;
  const uint32_t t = micros();
  this->do_update_();
  const uint32_t len = this->get_buffer_length_();
  for (uint32_t i = 0; i < len; i++)
    this->old_data_[i] = this->buffer_[i];
  this->old_data_valid_ = true;
  this->timing_.capture_us = micros() - t;
}

void GDEY075T7::setup() {
  // Same as WaveshareEPaperBase::setup_pins_() + initialize(), minus the
  // setup-time panel reset. That reset holds RESET low for reset_duration_
  // (200 ms default) plus 20 ms on every boot, which was measured as 234 of
  // the 278 ms ESPHome setup on a deep-sleep wake. It buys nothing here:
  // init_full_()/init_partial_() reset the controller before every refresh,
  // and leaving it untouched keeps the panel in its own deep sleep until then.
  this->init_internal_(this->get_buffer_length_());
  this->dc_pin_->setup();  // OUTPUT
  this->dc_pin_->digital_write(false);
  if (this->reset_pin_ != nullptr) {
    this->reset_pin_->setup();  // OUTPUT
    this->reset_pin_->digital_write(true);
  }
  if (this->busy_pin_ != nullptr) {
    this->busy_pin_->setup();  // INPUT
  }
  this->spi_setup();
  // After a deep-sleep wake the panel is still in its own deep sleep and
  // ignores commands until the next refresh resets it, so deep_sleep() must
  // not try to talk to it. (After a power-on it idles in standby instead,
  // until the first refresh; firmware should always draw on a cold boot.)
  this->panel_asleep_ = true;
  this->initialize();
}

void GDEY075T7::initialize() {
  RAMAllocator<uint8_t> allocator;
  this->old_data_ = allocator.allocate(this->get_buffer_length_());
  if (this->old_data_ == nullptr) {
    ESP_LOGE(TAG, "Failed to allocate old data buffer");
    this->mark_failed();
  }
}

void GDEY075T7::display() {
  // Guard against a failed old-data allocation: full_refresh() calls update() ->
  // display() directly, bypassing the scheduler's failed-component skip.
  if (this->old_data_ == nullptr)
    return;

  this->timing_.light_sleep_us = 0;
  this->timing_.light_sleep_count = 0;

  if (!this->old_data_valid_ && this->at_update_ != 0) {
    ESP_LOGD(TAG, "No valid previous frame; forcing full refresh");
    this->at_update_ = 0;
  }
  const bool full = this->at_update_ == 0;
  this->last_full_ = full;
  ESP_LOGD(TAG, "display() #%u/%u: %s refresh", (unsigned) this->at_update_, (unsigned) this->full_update_every_,
           full ? "FULL" : "partial");
  // full_update_every_ == 0: never schedule a full refresh (one still happens
  // when there is no valid previous frame).
  this->at_update_ = this->full_update_every_ == 0 ? 1 : (this->at_update_ + 1) % this->full_update_every_;

  bool ok;
  if (full) {
    // Flashing full-refresh waveform: slowest (~3.5 s) but best de-ghosting.
    this->init_full_();
    ok = this->write_frame_();
  } else {
    // Differential partial waveform: no flicker, fastest (~0.34 s). Accumulates
    // ghosting, which the periodic full refresh clears. The fast path
    // (init_fast_) is retained but currently unused.
    this->init_partial_();
    ok = this->write_partial_();
  }

  // Only clear the warning when the refresh actually succeeded; otherwise the
  // timeout warning set during the refresh must survive.
  if (ok)
    this->status_clear_warning();

  this->deep_sleep();
}

void GDEY075T7::init_full_() {
  reset_();

  this->wait_until_idle_();

  this->command(0x01);  // POWER SETTING
  this->data(0x07);
  this->data(0x07);  // VGH=20V,VGL=-20V
  this->data(0x3f);  // VDH=15V
  this->data(0x3f);  // VDL=-15V

  // Enhanced display drive(Add 0x06 command)
  this->command(0x06);  // Booster Soft Start
  this->data(0x17);
  this->data(0x17);
  this->data(0x28);
  this->data(0x17);

  this->command(0x04);  // POWER ON
  delay(100);
  this->wait_until_idle_();  // waiting for the electronic paper IC to
                             // release the idle signal

  this->command(0x00);  // PANNEL SETTING
  this->data(0x1F);     // KW-3f   KWR-2F BWROTP 0f BWOTP 1f

  this->command(0x61);  // tres
  this->data(0x03);     // source 800
  this->data(0x20);
  this->data(0x01);  // gate 480
  this->data(0xE0);

  this->command(0x15);
  this->data(0x00);

  this->command(0x50);  // VCOM AND DATA INTERVAL SETTING
  this->data(0x10);     // DDX[0]=0: full-refresh data polarity (new bit 1 = black)
  this->data(0x07);

  this->command(0x60);  // TCON SETTING
  this->data(0x22);
}

void GDEY075T7::init_fast_() {
  reset_();

  this->command(0x00);  // PANNEL SETTING
  this->data(0x1F);     // KW-3f   KWR-2F BWROTP 0f BWOTP 1f

  this->command(0x50);  // VCOM AND DATA INTERVAL SETTING
  this->data(0x10);     // DDX[0]=0: same data polarity as the full-refresh path
  this->data(0x07);

  this->command(0x04);  // POWER ON
  delay(100);
  this->wait_until_idle_();  // waiting for the electronic paper IC to
                             // release the idle signal

  // Enhanced display drive(Add 0x06 command)
  this->command(0x06);  // Booster Soft Start
  this->data(0x27);
  this->data(0x27);
  this->data(0x18);
  this->data(0x17);

  this->command(0xE0);  // Cascade setting
  this->data(0x02);
  this->command(0xE5);  // Force temperature -> 0x5A selects the ~1.5 s waveform
  this->data(0x5A);
}

void GDEY075T7::init_partial_() {
  uint32_t t = micros();
  reset_();
  this->timing_.reset_us = micros() - t;

  // Re-apply the power/booster settings: reset_() + the per-cycle deep sleep
  // revert them to power-on defaults, and a partial refresh must drive the panel
  // with the same voltages as a full refresh.
  this->command(0x01);  // POWER SETTING
  this->data(0x07);
  this->data(0x07);  // VGH=20V,VGL=-20V
  this->data(0x3f);  // VDH=15V
  this->data(0x3f);  // VDL=-15V

  this->command(0x06);  // Booster Soft Start
  this->data(0x17);
  this->data(0x17);
  this->data(0x28);
  this->data(0x17);

  this->command(0x00);  // PANNEL SETTING
  this->data(0x1F);     // KW-3f   KWR-2F BWROTP 0f BWOTP 1f

  t = micros();
  this->command(0x04);  // POWER ON
  if (this->light_sleep_waits_) {
    this->light_sleep_(100, false);
  } else {
    delay(100);
  }
  const uint32_t t_wait = micros();
  this->wait_idle_();  // waiting for the electronic paper IC to
                       // release the idle signal
  this->timing_.power_on_wait_us = micros() - t_wait;
  this->timing_.power_on_us = micros() - t;

  this->command(0xE0);  // Cascade setting
  this->data(0x02);
  this->command(0xE5);  // Force temperature -> select fast partial waveform
  this->data(0x6E);
}

bool GDEY075T7::write_frame_() {
  const uint32_t len = this->get_buffer_length_();

  // OLD data = 0x00: fixed reference for the full-refresh LUT.
  this->command(0x10);
  this->start_data_();
  for (uint32_t i = 0; i < len; i++)
    this->write_byte(0x00);
  this->end_data_();

  // NEW data = image, uninverted. Empirically confirmed on this exact panel:
  // a black square drawn at (0,0) rendered white in the correct position,
  // i.e. pure polarity inversion with correct geometry - the driver's
  // original assumption that the panel needs bit1=white inverted to
  // bit1=black was backwards for this unit.
  this->command(0x13);
  this->start_data_();
  for (uint32_t i = 0; i < len; i++)
    this->write_byte(this->buffer_[i]);
  this->end_data_();

  // Remember the frame just shown as the differential base for partial updates.
  for (uint32_t i = 0; i < len; i++)
    this->old_data_[i] = this->buffer_[i];
  this->old_data_valid_ = true;

  return this->refresh_();
}

bool GDEY075T7::write_partial_() {
  const uint32_t len = this->get_buffer_length_();

  this->command(0x50);  // VCOM AND DATA INTERVAL SETTING
  // 0x10 (DDX[0]=0), matching init_full_'s value for this same register -
  // the original 0xA9 (DDX[0]=1) applied an extra hardware-level polarity
  // inversion on top of a buffer_ that (per the write_frame_ fix, empirically
  // confirmed with a test pattern) already needs no inversion, so partial
  // refreshes came out inverted relative to full ones. Unverified beyond
  // that reasoning - watch the next partial-refresh render to confirm.
  this->data(0x10);
  this->data(0x07);

  this->command(0x91);  // This command makes the display enter partial mode
  this->command(0x90);  // resolution setting
  this->data(0x00);
  this->data(0x00);  // x-start
  this->data(WIDTH / 256);
  this->data(WIDTH % 256 - 1);  // x-end
  this->data(0x00);             //
  this->data(0x00);             // y-start
  this->data(HEIGHT / 256);
  this->data(HEIGHT % 256 - 1);  // y-end
  this->data(0x01);

  uint32_t t = micros();
  this->command(0x10);  // OLD data = previous frame
  this->start_data_();
  this->write_array(this->old_data_, len);
  this->end_data_();
  this->timing_.old_spi_us = micros() - t;

  // NEW data = current frame, written as-is (see the 0x50 comment above and
  // write_frame_'s comment - this panel needs no polarity inversion at all).
  t = micros();
  this->command(0x13);
  this->start_data_();
  this->write_array(this->buffer_, len);
  this->end_data_();
  this->timing_.new_spi_us = micros() - t;

  for (uint32_t i = 0; i < len; i++)
    this->old_data_[i] = this->buffer_[i];
  this->old_data_valid_ = true;

  t = micros();
  const bool ok = this->refresh_();
  this->timing_.refresh_us = micros() - t;

  this->command(0x92);  // exit partial mode AFTER the refresh

  return ok;
}

bool GDEY075T7::refresh_() {
  this->command(0x12);  // DISPLAY update
  delay(1);             //!!!The delay here is necessary, 200uS at least!!!
  if (!this->wait_idle_()) {  // waiting for the electronic paper IC to
                                    // release the idle signal
    this->status_set_warning();
    return false;
  }
  return true;
}

bool GDEY075T7::wait_idle_() {
  // BUSY_N reads low while the controller is busy (see is_busy_pin_inverted_()).
  if (this->light_sleep_waits_ && this->busy_pin_ != nullptr && this->busy_pin_->is_internal() &&
      !this->busy_pin_->digital_read()) {
    this->light_sleep_(this->idle_timeout_(), true);
  }
  // Also covers the timer-backstop case and any residual busy time.
  return this->wait_until_idle_();
}

void GDEY075T7::light_sleep_(uint32_t max_ms, bool wake_on_idle) {
  gpio_num_t busy = GPIO_NUM_NC;
  if (wake_on_idle) {
    busy = (gpio_num_t) static_cast<InternalGPIOPin *>(this->busy_pin_)->get_pin();
    gpio_wakeup_enable(busy, GPIO_INTR_HIGH_LEVEL);
    esp_sleep_enable_gpio_wakeup();
  }
  esp_sleep_enable_timer_wakeup((uint64_t) max_ms * 1000);
  this->hold_pins_(true);

  const uint32_t t = micros();
  esp_light_sleep_start();
  this->timing_.light_sleep_us += micros() - t;
  this->timing_.light_sleep_count++;

  this->hold_pins_(false);
  esp_sleep_disable_wakeup_source(ESP_SLEEP_WAKEUP_TIMER);
  if (wake_on_idle) {
    esp_sleep_disable_wakeup_source(ESP_SLEEP_WAKEUP_GPIO);
    gpio_wakeup_disable(busy);
  }
}

void GDEY075T7::hold_pins_(bool hold) {
  GPIOPin *pins[] = {this->reset_pin_, this->dc_pin_, this->cs_};
  for (GPIOPin *pin : pins) {
    if (pin == nullptr || !pin->is_internal())
      continue;
    const auto num = (gpio_num_t) static_cast<InternalGPIOPin *>(pin)->get_pin();
    if (hold) {
      gpio_hold_en(num);
    } else {
      gpio_hold_dis(num);
    }
  }
}

void GDEY075T7::reset_() {
  this->panel_asleep_ = false;
  if (this->reset_pin_ != nullptr) {
    this->reset_pin_->digital_write(false);
    delay(10);
    this->reset_pin_->digital_write(true);
    delay(10);
  }
}

void GDEY075T7::deep_sleep() {
  if (this->panel_asleep_)
    return;
  this->command(0x50);  // VCOM AND DATA INTERVAL SETTING
  this->data(0xf7);     // WBmode:VBDF 17|D7 VBDW 97 VBDB 57    WBRmode:VBDF F7
                        // VBDW 77 VBDB 37  VBDR B7

  const uint32_t t = micros();
  this->command(0x02);            // power off
  const bool idle = this->wait_idle_();  // waiting for the electronic paper IC to
                                         // release the idle signal
  this->timing_.power_off_us = micros() - t;
  if (!idle)
    return;

  this->command(0x07);  // deep sleep
  this->data(0xA5);
  this->panel_asleep_ = true;
}

void GDEY075T7::dump_config() {
  LOG_DISPLAY("", "Good Display e-Paper", this)
  ESP_LOGCONFIG(TAG, "  Model: GDEY075T7");
  LOG_PIN("  CS Pin: ", this->cs_);
  LOG_PIN("  Reset Pin: ", this->reset_pin_);
  LOG_PIN("  DC Pin: ", this->dc_pin_);
  LOG_PIN("  Busy Pin: ", this->busy_pin_);
  LOG_UPDATE_INTERVAL(this);
}

}  // namespace waveshare_epaper
}  // namespace esphome
