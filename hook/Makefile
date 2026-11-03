CFLAGS ?= -O2 -g -Wall -Wextra -Wno-unused-function -Wno-format-truncation

# Build to a temp file and rename: overwriting the .so in place would crash a running game that has it mapped.
librome_ai.so: rome_hook.c game.h minijson.h
	$(CC) $(CFLAGS) -fPIC -shared -o $@.tmp rome_hook.c -ldl -lpthread -lm
	mv -f $@.tmp $@

clean:
	rm -f librome_ai.so librome_ai.so.tmp

.PHONY: clean
