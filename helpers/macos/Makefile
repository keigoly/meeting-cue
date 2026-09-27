# macOS Swift ヘルパーのビルド。swiftc は Xcode Command Line Tools に同梱。
SWIFTC ?= swiftc
FLAGS  ?= -parse-as-library -O

all: stt_helper/stt-helper tap_helper/tap-helper hotkey_helper/hotkey-helper overlay_helper/overlay-helper mix_helper/mix-helper

stt_helper/stt-helper: stt_helper/main.swift
	$(SWIFTC) $(FLAGS) $< -o $@

tap_helper/tap-helper: tap_helper/main.swift
	$(SWIFTC) $(FLAGS) $< -o $@

hotkey_helper/hotkey-helper: hotkey_helper/main.swift
	$(SWIFTC) $(FLAGS) $< -o $@

overlay_helper/overlay-helper: overlay_helper/main.swift
	$(SWIFTC) $(FLAGS) $< -o $@

mix_helper/mix-helper: mix_helper/main.swift
	$(SWIFTC) $(FLAGS) $< -o $@

clean:
	rm -f stt_helper/stt-helper tap_helper/tap-helper hotkey_helper/hotkey-helper overlay_helper/overlay-helper mix_helper/mix-helper

.PHONY: all clean
