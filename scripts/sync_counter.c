#define _GNU_SOURCE

#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <pthread.h>
#include <stdint.h>
#include <stddef.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#define COUNTER_SIZE 4096U
#define HEADER_SIZE 256U
#define SLOT_SIZE 192U
#define SLOT_COUNT 20U
#define OPERATION_COUNT 6U
#define OUTCOME_COUNT 3U
#define SLOT_READY 2U

#define ENABLE_ENV "_AGENT_BUILDER_QUALIFICATION_SYNC_COUNTER"
#define FILE_ENV "_AGENT_BUILDER_SYNC_COUNTER_FILE"
#define ROLE_ENV "_AGENT_BUILDER_SYNC_COUNTER_ROLE"
#define REQUIRED_ENV "_AGENT_BUILDER_SYNC_COUNTER_REQUIRED"
#define SELFTEST_ENV "_AGENT_BUILDER_SYNC_COUNTER_SELFTEST_NO_GLOBAL_SYNC"

enum operation {
    OP_FSYNC = 0,
    OP_FDATASYNC = 1,
    OP_MSYNC = 2,
    OP_SYNCFS = 3,
    OP_SYNC_FILE_RANGE = 4,
    OP_SYNC = 5,
};

enum outcome {
    OUTCOME_ATTEMPT = 0,
    OUTCOME_SUCCESS = 1,
    OUTCOME_FAILURE = 2,
};

struct counter_header {
    unsigned char magic[8];
    uint32_t version;
    uint32_t file_size;
    uint32_t header_size;
    uint32_t slot_size;
    uint32_t slot_count;
    uint32_t operation_count;
    uint64_t registration_failures;
    uint64_t slot_overflow;
    uint64_t generation;
    unsigned char reserved[200];
};

struct counter_slot {
    uint32_t state;
    uint32_t role;
    int32_t pid;
    uint32_t reserved_identity;
    uint64_t instance_ns;
    uint64_t process_start_ticks;
    uint64_t counters[OPERATION_COUNT * OUTCOME_COUNT];
    unsigned char reserved[16];
};

struct counter_page {
    struct counter_header header;
    struct counter_slot slots[SLOT_COUNT];
};

_Static_assert(sizeof(struct counter_header) == HEADER_SIZE, "counter header ABI drift");
_Static_assert(sizeof(struct counter_slot) == SLOT_SIZE, "counter slot ABI drift");
_Static_assert(sizeof(struct counter_page) == COUNTER_SIZE, "counter page ABI drift");
_Static_assert(offsetof(struct counter_slot, counters) == 32U, "counter alignment drift");
_Static_assert(__atomic_always_lock_free(sizeof(uint32_t), 0), "32-bit atomics required");
_Static_assert(__atomic_always_lock_free(sizeof(uint64_t), 0), "64-bit atomics required");

typedef int (*one_fd_function)(int);
typedef int (*msync_function)(void *, size_t, int);
typedef int (*sync_file_range_function)(int, off64_t, off64_t, unsigned int);
typedef void (*sync_function)(void);

static one_fd_function real_fsync;
static one_fd_function real_fdatasync;
static msync_function real_msync;
static one_fd_function real_syncfs;
static sync_file_range_function real_sync_file_range;
static sync_function real_sync;
static struct counter_page *counter_page;
static struct counter_slot *process_slot;
static int selftest_no_global_sync;

static void fatal_counter_error(void) {
    _exit(125);
}

static void resolve_function(void *destination, size_t destination_size, const char *name) {
    void *symbol = dlsym(RTLD_NEXT, name);
    if (symbol == NULL || destination_size != sizeof(symbol)) {
        fatal_counter_error();
    }
    memcpy(destination, &symbol, destination_size);
}

static uint64_t process_start_ticks(void) {
    char payload[1024];
    int descriptor = open("/proc/self/stat", O_RDONLY | O_CLOEXEC);
    if (descriptor < 0) {
        fatal_counter_error();
    }
    ssize_t size = read(descriptor, payload, sizeof(payload) - 1U);
    int saved_errno = errno;
    close(descriptor);
    errno = saved_errno;
    if (size <= 0 || (size_t)size >= sizeof(payload) - 1U) {
        fatal_counter_error();
    }
    payload[size] = '\0';
    char *cursor = strrchr(payload, ')');
    if (cursor == NULL) {
        fatal_counter_error();
    }
    cursor++;
    for (unsigned int field = 3U; field <= 22U; field++) {
        while (*cursor == ' ') {
            cursor++;
        }
        if (*cursor == '\0') {
            fatal_counter_error();
        }
        char *end = cursor;
        while (*end != '\0' && *end != ' ') {
            end++;
        }
        if (field == 22U) {
            char saved = *end;
            *end = '\0';
            errno = 0;
            char *numeric_end = NULL;
            unsigned long long value = strtoull(cursor, &numeric_end, 10);
            int invalid = errno != 0 || numeric_end == cursor ||
                          *numeric_end != '\0' || value == 0U;
            *end = saved;
            if (invalid) {
                fatal_counter_error();
            }
            return (uint64_t)value;
        }
        cursor = end;
    }
    fatal_counter_error();
    return 0U;
}

static uint32_t role_number(const char *role) {
    if (role != NULL && strcmp(role, "supervisor") == 0) {
        return 1U;
    }
    if (role != NULL && strcmp(role, "gateway") == 0) {
        return 2U;
    }
    if (role != NULL && strcmp(role, "worker") == 0) {
        return 3U;
    }
    if (role != NULL && strcmp(role, "selftest") == 0) {
        return 4U;
    }
    return 0U;
}

static void register_process_slot(void) {
    const char *enabled = getenv(ENABLE_ENV);
    const char *required = getenv(REQUIRED_ENV);
    const char *path = getenv(FILE_ENV);
    const char *role_text = getenv(ROLE_ENV);
    if (enabled == NULL || strcmp(enabled, "1") != 0 || required == NULL ||
        strcmp(required, "1") != 0 || path == NULL || path[0] != '/' ||
        strnlen(path, PATH_MAX + 1U) > PATH_MAX) {
        fatal_counter_error();
    }
    uint32_t role = role_number(role_text);
    if (role == 0U) {
        fatal_counter_error();
    }
    selftest_no_global_sync = role == 4U && getenv(SELFTEST_ENV) != NULL &&
                              strcmp(getenv(SELFTEST_ENV), "1") == 0;
    if (role != 4U && getenv(SELFTEST_ENV) != NULL) {
        fatal_counter_error();
    }

    int descriptor = open(path, O_RDWR | O_CLOEXEC | O_NOFOLLOW);
    if (descriptor < 0) {
        fatal_counter_error();
    }
    struct stat metadata;
    if (fstat(descriptor, &metadata) != 0 || !S_ISREG(metadata.st_mode) ||
        metadata.st_uid != geteuid() || metadata.st_nlink != 1 ||
        (metadata.st_mode & 0777U) != 0600U || metadata.st_size != COUNTER_SIZE) {
        close(descriptor);
        fatal_counter_error();
    }
    void *mapping = mmap(NULL, COUNTER_SIZE, PROT_READ | PROT_WRITE, MAP_SHARED, descriptor, 0);
    int saved_errno = errno;
    close(descriptor);
    errno = saved_errno;
    if (mapping == MAP_FAILED) {
        fatal_counter_error();
    }
    counter_page = mapping;
    static const unsigned char expected_magic[8] = {'A', 'B', 'S', 'Y', 'N', 'C', '1', '\0'};
    if (memcmp(counter_page->header.magic, expected_magic, sizeof(expected_magic)) != 0 ||
        counter_page->header.version != 1U || counter_page->header.file_size != COUNTER_SIZE ||
        counter_page->header.header_size != HEADER_SIZE ||
        counter_page->header.slot_size != SLOT_SIZE ||
        counter_page->header.slot_count != SLOT_COUNT ||
        counter_page->header.operation_count != OPERATION_COUNT) {
        fatal_counter_error();
    }

    int32_t pid = (int32_t)getpid();
    uint64_t start_ticks = process_start_ticks();
    for (unsigned int index = 0U; index < SLOT_COUNT; index++) {
        struct counter_slot *slot = &counter_page->slots[index];
        if (__atomic_load_n(&slot->state, __ATOMIC_ACQUIRE) == SLOT_READY &&
            slot->pid == pid && slot->role == role &&
            slot->process_start_ticks == start_ticks) {
            process_slot = slot;
            return;
        }
    }
    for (unsigned int index = 0U; index < SLOT_COUNT; index++) {
        struct counter_slot *slot = &counter_page->slots[index];
        uint32_t empty = 0U;
        if (!__atomic_compare_exchange_n(
                &slot->state, &empty, 1U, 0, __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE)) {
            continue;
        }
        struct timespec now;
        if (clock_gettime(CLOCK_MONOTONIC, &now) != 0) {
            __atomic_fetch_add(&counter_page->header.registration_failures, 1U, __ATOMIC_RELAXED);
            fatal_counter_error();
        }
        slot->role = role;
        slot->pid = pid;
        slot->reserved_identity = 0U;
        slot->instance_ns = (uint64_t)now.tv_sec * 1000000000ULL + (uint64_t)now.tv_nsec;
        slot->process_start_ticks = start_ticks;
        __atomic_store_n(&slot->state, SLOT_READY, __ATOMIC_RELEASE);
        process_slot = slot;
        return;
    }
    __atomic_fetch_add(&counter_page->header.slot_overflow, 1U, __ATOMIC_RELAXED);
    fatal_counter_error();
}

static void after_fork_child(void) {
    process_slot = NULL;
}

__attribute__((constructor)) static void initialise_sync_counter(void) {
    resolve_function(&real_fsync, sizeof(real_fsync), "fsync");
    resolve_function(&real_fdatasync, sizeof(real_fdatasync), "fdatasync");
    resolve_function(&real_msync, sizeof(real_msync), "msync");
    resolve_function(&real_syncfs, sizeof(real_syncfs), "syncfs");
    resolve_function(&real_sync_file_range, sizeof(real_sync_file_range), "sync_file_range");
    resolve_function(&real_sync, sizeof(real_sync), "sync");
    register_process_slot();
    if (pthread_atfork(NULL, NULL, after_fork_child) != 0) {
        __atomic_fetch_add(&counter_page->header.registration_failures, 1U, __ATOMIC_RELAXED);
        fatal_counter_error();
    }
}

static void record_result(enum operation operation, int succeeded) {
    if (process_slot == NULL) {
        return;
    }
    size_t base = (size_t)operation * OUTCOME_COUNT;
    __atomic_fetch_add(&process_slot->counters[base + OUTCOME_ATTEMPT], 1U, __ATOMIC_RELAXED);
    __atomic_fetch_add(
        &process_slot->counters[base + (succeeded ? OUTCOME_SUCCESS : OUTCOME_FAILURE)],
        1U,
        __ATOMIC_RELAXED);
}

int fsync(int descriptor) {
    int result = real_fsync(descriptor);
    int saved_errno = errno;
    record_result(OP_FSYNC, result == 0);
    errno = saved_errno;
    return result;
}

int fdatasync(int descriptor) {
    int result = real_fdatasync(descriptor);
    int saved_errno = errno;
    record_result(OP_FDATASYNC, result == 0);
    errno = saved_errno;
    return result;
}

int msync(void *address, size_t length, int flags) {
    int result = real_msync(address, length, flags);
    int saved_errno = errno;
    record_result(OP_MSYNC, result == 0);
    errno = saved_errno;
    return result;
}

int syncfs(int descriptor) {
    int result = real_syncfs(descriptor);
    int saved_errno = errno;
    record_result(OP_SYNCFS, result == 0);
    errno = saved_errno;
    return result;
}

int sync_file_range(int descriptor, off64_t offset, off64_t count, unsigned int flags) {
    int result = real_sync_file_range(descriptor, offset, count, flags);
    int saved_errno = errno;
    record_result(OP_SYNC_FILE_RANGE, result == 0);
    errno = saved_errno;
    return result;
}

void sync(void) {
    if (!selftest_no_global_sync) {
        real_sync();
    }
    int saved_errno = errno;
    record_result(OP_SYNC, 1);
    errno = saved_errno;
}
