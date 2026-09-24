// SPDX-License-Identifier: MPL-2.0

#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

int main(void)
{
	char path[64];
	char script[256];
	int fd;
	int status;
	int close_status;
	ssize_t written;
	pid_t child;

	snprintf(path, sizeof(path), "/tmp/shebang-argv-%ld.sh", (long)getpid());
	snprintf(script, sizeof(script),
		 "#!/bin/sh\n[ \"$0\" = \"%s\" ] && [ \"$1\" = payload ] && [ \"$#\" -eq 1 ]\n",
		 path);

	fd = open(path, O_CREAT | O_EXCL | O_WRONLY, 0755);
	if (fd < 0)
		return 1;
	written = write(fd, script, strlen(script));
	close_status = close(fd);
	if (written != (ssize_t)strlen(script) || close_status != 0) {
		unlink(path);
		return 1;
	}

	child = fork();
	if (child == 0) {
		char *const argv[] = { "caller-supplied-argv0", "payload", NULL };
		char *const envp[] = { NULL };

		execve(path, argv, envp);
		_exit(127);
	}
	if (child < 0) {
		unlink(path);
		return 1;
	}

	if (waitpid(child, &status, 0) != child) {
		unlink(path);
		return 1;
	}
	unlink(path);
	if (!WIFEXITED(status) || WEXITSTATUS(status) != 0) {
		fprintf(stderr, "shebang script received the wrong argument list\n");
		return 1;
	}

	puts("shebang argv regression passed.");
	return 0;
}
