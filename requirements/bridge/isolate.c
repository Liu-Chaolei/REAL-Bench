/* Private filesystem view for the unmodified Agent. No Landlock ABI1 policy. */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <linux/audit.h>
#include <linux/capability.h>
#include <linux/filter.h>
#include <linux/seccomp.h>
#include <sched.h>
#include <signal.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mount.h>
#include <linux/mount.h>
#include <sys/prctl.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <unistd.h>
#include <limits.h>

static volatile sig_atomic_t child_pid;
static void die(const char *what) { perror(what); exit(125); }
static void check(int result, const char *what) { if (result < 0) die(what); }
static void forward(int signal) { if (child_pid > 0) kill(child_pid, signal); }
static void signals(void) {
    struct sigaction action = { .sa_handler = forward };
    sigemptyset(&action.sa_mask);
    check(sigaction(SIGTERM, &action, NULL), "SIGTERM handler");
    check(sigaction(SIGINT, &action, NULL), "SIGINT handler");
}
static void write_mapping(const char *path, const char *value) {
    int fd = open(path, O_WRONLY | O_CLOEXEC);
    check(fd, "namespace mapping");
    size_t size = strlen(value);
    if (write(fd, value, size) != (ssize_t)size) die("namespace mapping write");
    close(fd);
}
static void directories(const char *path) {
    char copy[PATH_MAX];
    if (snprintf(copy, sizeof(copy), "%s", path) >= (int)sizeof(copy)) {
        errno = ENAMETOOLONG; die("view directory");
    }
    for (char *p = copy + 1; ; ++p) {
        if (*p != '/' && *p != '\0') continue;
        char saved = *p; *p = '\0';
        if (mkdir(copy, 0755) && errno != EEXIST) die("view mkdir");
        *p = saved;
        if (!saved) break;
    }
}
static void readonly(const char *path) {
    struct mount_attr attr = {
        .attr_set = MOUNT_ATTR_RDONLY | MOUNT_ATTR_NOSUID | MOUNT_ATTR_NODEV,
    };
    check(syscall(SYS_mount_setattr, AT_FDCWD, path, AT_RECURSIVE, &attr, sizeof(attr)),
          "readonly program tree");
}
static void bind_tree(const char *view, const char *source, int ro) {
    struct stat st;
    if (stat(source, &st)) { if (errno == ENOENT) return; die("source tree"); }
    char target[PATH_MAX];
    if (snprintf(target, sizeof(target), "%s%s", view, source) >= (int)sizeof(target)) {
        errno = ENAMETOOLONG; die("view bind path");
    }
    directories(target);
    check(mount(source, target, NULL, MS_BIND | MS_REC, NULL), "view bind");
    if (ro) readonly(target);
}
static void private_home(const char *view, const char *trial) {
    const char *home = getenv("HOME");
    if (!home || home[0] != '/' || !strcmp(home, "/") || strstr(home, "/../") || strstr(home, "/./")) {
        errno = EINVAL; die("absolute HOME path is required");
    }
    const char *reserved[] = {"/opt", "/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc", "/sys", "/proc", "/dev", "/tmp", NULL};
    for (int i = 0; reserved[i]; ++i) {
        size_t length = strlen(reserved[i]);
        if (!strncmp(home, reserved[i], length) && (!home[length] || home[length] == '/')) {
            errno = EINVAL; die("HOME overlaps the filesystem view");
        }
    }
    char *target, *source, *parent;
    check(asprintf(&target, "%s%s", view, home), "HOME alias target");
    check(asprintf(&source, "%s/home", trial), "HOME alias source");
    parent = strdup(target); if (!parent) die("HOME parent");
    *strrchr(parent, '/') = '\0'; directories(parent);
    check(symlink(source, target), "private HOME alias");
    free(target); free(source); free(parent);
}
/* Setup needs namespace-local capabilities. None reach Agent code or tools. */
static void drop_privileges(void) {
    for (int cap = 0; ; ++cap) {
        if (prctl(PR_CAPBSET_READ, cap, 0, 0, 0) < 0) {
            if (errno == EINVAL) break;
            die("capability query");
        }
        check(prctl(PR_CAPBSET_DROP, cap, 0, 0, 0), "capability bounding drop");
    }
    check(prctl(PR_CAP_AMBIENT, PR_CAP_AMBIENT_CLEAR_ALL, 0, 0, 0), "ambient capability drop");
    struct __user_cap_header_struct header = { .version = _LINUX_CAPABILITY_VERSION_3 };
    struct __user_cap_data_struct data[2] = {{0}, {0}};
    check(syscall(SYS_capset, &header, data), "capability drop");
    check(prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0), "no_new_privs");
}
#define DENY_SYSCALL(number) \
    BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, number, 0, 1), \
    BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | EPERM)
static void lock_view(void) {
#if defined(__x86_64__)
#define TAU_ARCH AUDIT_ARCH_X86_64
#elif defined(__aarch64__)
#define TAU_ARCH AUDIT_ARCH_AARCH64
#else
#error Unsupported architecture
#endif
    struct sock_filter filter[] = {
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, arch)),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, TAU_ARCH, 1, 0),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_KILL_PROCESS),
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, nr)),
#if defined(__x86_64__)
        BPF_JUMP(BPF_JMP | BPF_JSET | BPF_K, 0x40000000, 0, 1),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | ENOSYS),
#endif
        DENY_SYSCALL(SYS_unshare), DENY_SYSCALL(SYS_setns),
        DENY_SYSCALL(SYS_mount), DENY_SYSCALL(SYS_umount2),
        DENY_SYSCALL(SYS_pivot_root), DENY_SYSCALL(SYS_chroot),
        DENY_SYSCALL(SYS_fsopen), DENY_SYSCALL(SYS_fsconfig),
        DENY_SYSCALL(SYS_fsmount), DENY_SYSCALL(SYS_fspick),
        DENY_SYSCALL(SYS_open_tree), DENY_SYSCALL(SYS_move_mount),
        DENY_SYSCALL(SYS_mount_setattr),
        /* Match Docker's usual clone3 fallback, retaining pthread/fork support. */
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, SYS_clone3, 0, 1),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | ENOSYS),
        BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K, SYS_clone, 0, 3),
        BPF_STMT(BPF_LD | BPF_W | BPF_ABS, offsetof(struct seccomp_data, args[0])),
        BPF_JUMP(BPF_JMP | BPF_JSET | BPF_K,
                 CLONE_NEWUSER | CLONE_NEWNS | CLONE_NEWPID | CLONE_NEWNET |
                 CLONE_NEWIPC | CLONE_NEWUTS | CLONE_NEWCGROUP, 0, 1),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ERRNO | EPERM),
        BPF_STMT(BPF_RET | BPF_K, SECCOMP_RET_ALLOW),
    };
    struct sock_fprog program = { .len = sizeof(filter) / sizeof(filter[0]), .filter = filter };
    check(prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, &program), "view lock");
}
static int reap(pid_t main_child) {
    int status;
    for (;;) {
        pid_t pid = waitpid(-1, &status, 0);
        if (pid < 0) { if (errno == EINTR) continue; die("wait"); }
        if (pid == main_child) break;
    }
    return WIFEXITED(status) ? WEXITSTATUS(status) : 128 + WTERMSIG(status);
}
int main(int argc, char **argv) {
    if (argc < 5) { fputs("Usage: tau-isolate ROOT DENIED COMMAND [ARGS...]\n", stderr); return 125; }
    char trial[PATH_MAX];
    if (!realpath(argv[1], trial)) die("private trial root");
    if (strncmp(trial, "/tmp/tau-qwen-", 14)) { errno = EINVAL; die("private trial root prefix"); }
    int marker = open(argv[2], O_RDONLY | O_CLOEXEC);
    check(marker, "pre-isolation denied marker"); close(marker);
    uid_t uid = getuid(); gid_t gid = getgid();
    check(unshare(CLONE_NEWUSER), "user namespace; platform security options are required");
    char mapping[96];
    snprintf(mapping, sizeof(mapping), "%u %u 1", uid, uid);
    write_mapping("/proc/self/uid_map", mapping);
    write_mapping("/proc/self/setgroups", "deny");
    snprintf(mapping, sizeof(mapping), "%u %u 1", gid, gid);
    write_mapping("/proc/self/gid_map", mapping);
    check(unshare(CLONE_NEWNS | CLONE_NEWPID), "mount and PID namespaces");
    check(mount(NULL, "/", NULL, MS_REC | MS_PRIVATE, NULL), "private mount propagation");
    char view[] = "/tmp/tau-qwen-view-XXXXXX";
    if (!mkdtemp(view)) die("view root");
    check(mount("tmpfs", view, "tmpfs", MS_NOSUID | MS_NODEV, "mode=0755"), "view tmpfs");
    const char *roots[] = {"/opt", "/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc", "/sys", NULL};
    for (int i = 0; roots[i]; ++i) bind_tree(view, roots[i], 1);
    bind_tree(view, "/dev", 0);
    bind_tree(view, trial, 0);
    private_home(view, trial);
    char proc[PATH_MAX]; snprintf(proc, sizeof(proc), "%s/proc", view); directories(proc);
    signals();
    pid_t init = fork(); check(init, "PID namespace init");
    if (!init) {
        child_pid = 0;
        check(mount("proc", proc, "proc", MS_RDONLY | MS_NOSUID | MS_NODEV | MS_NOEXEC, NULL), "private proc");
        check(mount(NULL, view, NULL, MS_REMOUNT | MS_RDONLY | MS_NOSUID | MS_NODEV, NULL), "readonly view root");
        check(chroot(view), "view chroot"); check(chdir("/"), "view cwd");
        drop_privileges(); lock_view();
        /* Do not inherit any handle to a path outside the view. */
        check(syscall(SYS_close_range, 3, ~0U, 0), "close external handles");
        check(setenv("TAU_FILESYSTEM_BOUNDARY", "namespace-v1", 1), "boundary marker");
        pid_t agent = fork(); check(agent, "Agent fork");
        if (!agent) {
            char *workspace;
            check(asprintf(&workspace, "%s/workspace", trial), "workspace path");
            check(chdir(workspace), "Agent cwd");
            execvp(argv[3], &argv[3]); die("Agent exec");
        }
        child_pid = agent;
        return reap(agent);
    }
    child_pid = init;
    int result = reap(init);
    check(umount2(view, MNT_DETACH), "view cleanup");
    check(rmdir(view), "view directory cleanup");
    return result;
}
