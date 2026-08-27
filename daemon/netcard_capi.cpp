/*
 * Thin C-linkage wrapper around Linux_NetCard so Python (via ctypes) can
 * call the original, tested send_frame() implementation directly --
 * synchronously, in-process -- instead of through the daemon's polling
 * loop (main.cpp's channel_thread checks a shared-memory command byte
 * every usleep(1000000/FPS), i.e. up to ~16.7ms of avoidable latency per
 * frame at 60Hz, on top of jitter from however that timing lines up
 * frame to frame).
 *
 * rows/cols/buffer are protected on Linux_NetCard, so this exposes them
 * through a trivial derived class rather than modifying the original
 * header.
 */

#include <cerrno>
#include "Linux_NetCard.h"

using namespace Matrix;

namespace {
	class ExposedNetCard : public Linux_NetCard {
		public:
			using Linux_NetCard::Linux_NetCard;
			uint32_t get_rows() const { return rows; }
			uint32_t get_cols() const { return cols; }
			unsigned char *get_buffer() const { return reinterpret_cast<unsigned char *>(buffer); }
	};
}

extern "C" {

void *netcard_create(const char *iface, uint32_t channel, uint32_t rows, uint32_t cols) {
	try {
		return new ExposedNetCard(iface, channel, rows, cols);
	} catch (int err) {
		errno = err;
		return nullptr;
	} catch (...) {
		errno = EINVAL;
		return nullptr;
	}
}

void netcard_destroy(void *handle) {
	delete static_cast<ExposedNetCard *>(handle);
}

unsigned char *netcard_buffer(void *handle) {
	return static_cast<ExposedNetCard *>(handle)->get_buffer();
}

uint32_t netcard_rows(void *handle) {
	return static_cast<ExposedNetCard *>(handle)->get_rows();
}

uint32_t netcard_cols(void *handle) {
	return static_cast<ExposedNetCard *>(handle)->get_cols();
}

int netcard_send_frame(void *handle, int vlan, uint16_t vlan_id) {
	try {
		static_cast<ExposedNetCard *>(handle)->send_frame(vlan != 0, vlan_id);
		return 0;
	} catch (int err) {
		errno = err;
		return -1;
	} catch (...) {
		errno = EINVAL;
		return -1;
	}
}

void netcard_set_brightness(void *handle, uint8_t brightness) {
	static_cast<ExposedNetCard *>(handle)->set_brightness(brightness);
}

}
