// SPDX-License-Identifier: MPL-2.0
// Does the kernel reject a cycle of two epoll sets watching each other?
//
// Linux returns ELOOP for the second `EPOLL_CTL_ADD`. A kernel that only
// checks self-reference and nesting depth accepts it, leaving two sets that
// monitor each other.

#include <errno.h>
#include <stdio.h>
#include <sys/epoll.h>
#include <unistd.h>

int main(void)
{
	struct epoll_event ev = { .events = EPOLLIN };
	int a = epoll_create1(0);
	int b = epoll_create1(0);
	int p[2];

	setvbuf(stdout, NULL, _IONBF, 0);
	if (a < 0 || b < 0 || pipe(p) < 0) {
		perror("setup");
		return 1;
	}

	ev.data.fd = b;
	int r1 = epoll_ctl(a, EPOLL_CTL_ADD, b, &ev);
	printf("EPCYCLE add(a<-b)=%d errno=%d\n", r1, r1 < 0 ? errno : 0);

	ev.data.fd = a;
	errno = 0;
	int r2 = epoll_ctl(b, EPOLL_CTL_ADD, a, &ev);
	printf("EPCYCLE add(b<-a)=%d errno=%d (ELOOP=%d)\n", r2, r2 < 0 ? errno : 0, ELOOP);

	/* The cycle is only observable if something makes one side ready. */
	ev.data.fd = p[0];
	if (epoll_ctl(a, EPOLL_CTL_ADD, p[0], &ev) < 0)
		perror("add pipe to a");
	write(p[1], "x", 1);

	struct epoll_event out[4];
	int n = epoll_wait(a, out, 4, 200);
	printf("EPCYCLE wait n=%d errno=%d\n", n, n < 0 ? errno : 0);

	/* A cycle can keep both sides ready: count immediate returns. */
	int busy = 0;
	for (int i = 0; i < 1000; i++) {
		int m = epoll_wait(a, out, 4, 0);
		if (m > 0)
			busy++;
	}
	printf("EPCYCLE busy_returns=%d/1000\n", busy);

	return 0;
}
