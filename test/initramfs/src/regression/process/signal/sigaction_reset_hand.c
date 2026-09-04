// SPDX-License-Identifier: MPL-2.0

#include <signal.h>
#include <sys/wait.h>
#include <unistd.h>

#include "../../common/test.h"

static volatile sig_atomic_t handler_called;
static volatile sig_atomic_t handler_got_siginfo;
static volatile sig_atomic_t handler_saw_default;
static volatile sig_atomic_t handler_saw_siginfo;

static void reset_hand_handler(int signum, siginfo_t *info, void *context)
{
	struct sigaction current;

	(void)signum;
	(void)context;
	handler_called = 1;
	handler_got_siginfo = info != NULL;
	if (sigaction(SIGUSR1, NULL, &current) == 0) {
		handler_saw_default = current.sa_handler == SIG_DFL;
		handler_saw_siginfo = (current.sa_flags & SA_SIGINFO) != 0;
	}
}

FN_TEST(reset_hand_preserves_action_metadata)
{
	struct sigaction action = { 0 };
	struct sigaction current;
	struct sigaction old_action;
	int status;
	pid_t child;

	action.sa_sigaction = reset_hand_handler;
	action.sa_flags = SA_RESETHAND | SA_SIGINFO;
	CHECK(sigemptyset(&action.sa_mask));

	TEST_SUCC(sigaction(SIGUSR1, &action, &old_action));
	TEST_RES(raise(SIGUSR1), handler_called && handler_got_siginfo &&
					 handler_saw_default &&
					 handler_saw_siginfo);
	TEST_RES(sigaction(SIGUSR1, NULL, &current),
		 current.sa_handler == SIG_DFL &&
			 (current.sa_flags & SA_SIGINFO) != 0);

	child = TEST_SUCC(fork());
	if (child == 0) {
		raise(SIGUSR1);
		_exit(EXIT_SUCCESS);
	}
	TEST_RES(waitpid(child, &status, 0),
		 _ret == child && WIFSIGNALED(status) &&
			 WTERMSIG(status) == SIGUSR1);

	TEST_SUCC(sigaction(SIGUSR1, &old_action, NULL));
}
END_TEST()
