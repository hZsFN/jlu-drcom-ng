CC      ?= cc
CFLAGS  ?= -O2 -Wall -Wextra -std=c99 -D_GNU_SOURCE
VERSION := $(shell sed -n 's/^__version__ *= *"\([^"]*\)"/\1/p' ../drcom/__init__.py 2>/dev/null || echo 2.2.0)
CFLAGS  += -DDRCOMD_VERSION=\"$(VERSION)\"
LDFLAGS ?= -lpthread -lcrypto

CORE_SRCS = md4.c md5.c sha1.c protocol.c engine.c config_load.c main.c
CORE_HDRS = platform.h protocol.h engine.h config.h md4.h md5.h sha1.h

.PHONY: all test clean

all: drcomd

drcomd: $(CORE_SRCS) $(CORE_HDRS)
	$(CC) $(CFLAGS) $(CORE_SRCS) -o $@ $(LDFLAGS)

tests/test_hash: md4.c md5.c sha1.c tests/test_hash.c
	$(CC) $(CFLAGS) $^ -o $@

tests/test_protocol: md4.c md5.c sha1.c protocol.c tests/test_protocol.c
	$(CC) $(CFLAGS) $^ -o $@

tests/test_engine: md4.c md5.c sha1.c protocol.c engine.c tests/test_engine.c
	$(CC) $(CFLAGS) $^ -o $@ -lpthread

test: drcomd tests/test_hash tests/test_protocol tests/test_engine
	./tests/test_hash
	./tests/test_protocol

clean:
	rm -f drcomd tests/test_hash tests/test_protocol tests/test_engine *.o *.res
