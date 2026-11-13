#pragma once
// One-entry DNS cache in RTC memory, served through lwIP's external-resolve
// hook (sdkconfig: CONFIG_LWIP_HOOK_NETCONN_EXT_RESOLVE_CUSTOM). Every
// getaddrinfo() in the firmware, including http_request's and online_image's,
// goes through the hook, so a cached name costs no DNS round trip at all.
// Only name resolution is bypassed: TLS still verifies the certificate
// against the hostname in the URL.
//
// Why: a DNS lookup right after connecting took 40-660 ms on the bench
// (2026-09-28), even though the router answers in ~2 ms, and the record's
// TTL is 0, so nothing upstream caches it either. The server's address is
// stable, so the cache ignores the TTL and is invalidated instead: by any
// connection failure (invalidate()), and by power loss or reboot (RTC memory).

#include <cstdint>
#include <cstring>

#include "esp_attr.h"
#include "lwip/api.h"
#include "lwip/err.h"
#include "lwip/ip_addr.h"
#include "lwip/netdb.h"

namespace dns_cache {

static constexpr uint32_t MAGIC = 0x444E5343;  // "DNSC"

struct Entry {
  uint32_t magic;
  uint32_t ipv4;  // network byte order, as in ip4_addr_t::addr
  char host[64];
};

static RTC_DATA_ATTR Entry entry;

inline bool cached(const char *host) {
  return entry.magic == MAGIC && strncmp(entry.host, host, sizeof(entry.host)) == 0;
}

inline void invalidate() { entry.magic = 0; }

// Make sure host is cached, doing a real lookup only if it isn't. Returns
// false if the lookup failed (the entry stays empty; the next attempt retries).
inline bool ensure(const char *host) {
  if (cached(host))
    return true;
  if (strlen(host) >= sizeof(entry.host))
    return false;
  invalidate();  // so the hook below doesn't answer from a stale entry
  struct addrinfo hints {};
  hints.ai_family = AF_INET;
  struct addrinfo *res = nullptr;
  if (getaddrinfo(host, nullptr, &hints, &res) != 0 || res == nullptr)
    return false;
  entry.ipv4 = reinterpret_cast<struct sockaddr_in *>(res->ai_addr)->sin_addr.s_addr;
  freeaddrinfo(res);
  strncpy(entry.host, host, sizeof(entry.host));
  entry.magic = MAGIC;
  return true;
}

}  // namespace dns_cache

// lwIP calls this before sending any DNS query. Returning 1 means "resolved
// here"; 0 falls through to normal DNS.
extern "C" int lwip_hook_netconn_external_resolve(const char *name, ip_addr_t *addr, u8_t addrtype,
                                                  err_t *err) {
  if (addrtype == NETCONN_DNS_IPV6 || !dns_cache::cached(name))
    return 0;
  ip4_addr_t ip4;
  ip4.addr = dns_cache::entry.ipv4;
  ip_addr_copy_from_ip4(*addr, ip4);
  *err = ERR_OK;
  return 1;
}
