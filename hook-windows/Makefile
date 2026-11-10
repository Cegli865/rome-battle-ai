# Windows build of the hook, cross-compiled on Linux with MinGW-w64.
# The game loads it as dinput8.dll (see README).
CC      = x86_64-w64-mingw32-gcc
CFLAGS  = -O2 -g -Wall -Wextra -Wno-unused-function -Wno-format-truncation -Wno-cast-function-type
LDFLAGS = -shared -static-libgcc -Wl,--enable-stdcall-fixup -Wl,--kill-at
LIBS    = -lws2_32

dinput8.dll: rome_hook_win.c game_win.h minijson.h
	$(CC) $(CFLAGS) $(LDFLAGS) -o $@.tmp rome_hook_win.c $(LIBS)
	mv -f $@.tmp $@

clean:
	rm -f dinput8.dll dinput8.dll.tmp
