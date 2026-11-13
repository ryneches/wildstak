#pragma once
// Flash cache of rendered card framebuffers (v0.3; NOTES.md, "State: where
// each thing lives"). One fixed slot per card, slot i = manifest card i, in
// the "cards" data partition (partitions_cards.csv).
//
// A slot holds the panel framebuffer itself (what GDEY075T7::frame_buffer()
// contains after rendering the card), not the decoded PNG, so a rotation wake
// reads it straight into the display driver's buffers with no rendering.
//
// Slot layout: [frame: FRAME_BYTES][Header] in one 64 KB flash block. The
// header is written last, so a power cut mid-write leaves the slot without a
// valid magic (treated as empty) rather than half-written.

#include <cstdint>

#include "esp_partition.h"
#include "esp_rom_crc.h"

namespace card_cache {

static constexpr uint32_t FRAME_BYTES = 48000;   // 800 x 480, 1 bpp
static constexpr uint32_t SLOT_BYTES = 0x10000;  // one 64 KB erase block
static constexpr uint32_t HEADER_OFFSET = FRAME_BYTES;
static constexpr uint32_t MAGIC = 0x43415244;  // "CARD"
static constexpr uint8_t PARTITION_SUBTYPE = 0x40;

struct Header {
  uint32_t magic;
  uint32_t crc;       // CRC32 of the frame bytes
  uint64_t src_hash;  // manifest hash of the source PNG; 0 = unknown
};

inline const esp_partition_t *partition() {
  static const esp_partition_t *p = esp_partition_find_first(
      ESP_PARTITION_TYPE_DATA, (esp_partition_subtype_t) PARTITION_SUBTYPE, "cards");
  return p;
}

inline int slot_count() {
  const esp_partition_t *p = partition();
  return p != nullptr ? (int) (p->size / SLOT_BYTES) : 0;
}

inline uint32_t frame_crc(const uint8_t *frame) { return esp_rom_crc32_le(0, frame, FRAME_BYTES); }

inline bool read_header(int slot, Header &h) {
  if (slot < 0 || slot >= slot_count())
    return false;
  if (esp_partition_read(partition(), slot * SLOT_BYTES + HEADER_OFFSET, &h, sizeof(h)) != ESP_OK)
    return false;
  return h.magic == MAGIC;
}

// Read a slot's frame into dst (FRAME_BYTES). False if the slot is empty,
// unreadable, or fails its CRC.
inline bool read_frame(int slot, uint8_t *dst) {
  Header h;
  if (!read_header(slot, h))
    return false;
  if (esp_partition_read(partition(), slot * SLOT_BYTES, dst, FRAME_BYTES) != ESP_OK)
    return false;
  return frame_crc(dst) == h.crc;
}

enum WriteResult : uint8_t { WRITE_FAILED = 0, WRITE_UNCHANGED = 1, WRITE_DONE = 2 };

// Store a frame unless the slot already holds identical pixels under the same
// source hash. Same pixels with a new hash is still rewritten (NOR flash can
// only update the header by erasing the block), so the next sync can skip the
// download.
inline WriteResult write_frame(int slot, const uint8_t *frame, uint64_t src_hash) {
  if (slot < 0 || slot >= slot_count())
    return WRITE_FAILED;
  const uint32_t crc = frame_crc(frame);
  Header old;
  if (read_header(slot, old) && old.crc == crc && old.src_hash == src_hash)
    return WRITE_UNCHANGED;

  const esp_partition_t *p = partition();
  const uint32_t base = slot * SLOT_BYTES;
  if (esp_partition_erase_range(p, base, SLOT_BYTES) != ESP_OK)
    return WRITE_FAILED;
  if (esp_partition_write(p, base, frame, FRAME_BYTES) != ESP_OK)
    return WRITE_FAILED;
  const Header h{MAGIC, crc, src_hash};
  if (esp_partition_write(p, base + HEADER_OFFSET, &h, sizeof(h)) != ESP_OK)
    return WRITE_FAILED;
  return WRITE_DONE;
}

}  // namespace card_cache
