// SPDX-License-Identifier: MPL-2.0

// The USB enumeration probe.
//
// Boots as the initramfs init, mounts devtmpfs/sysfs and watches
// /sys/bus/usb/devices for an enumerated device. It announces every entry it
// finds with its vendor and product id, then prints USBPROBE_PASS and keeps
// the guest alive while the host adds and removes the stick.

#define _GNU_SOURCE

#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

static int read_attr(const char *dir, const char *attr, char *buffer, size_t size) {
    char path[512];
    snprintf(path, sizeof(path), "/sys/bus/usb/devices/%s/%s", dir, attr);

    int fd = open(path, O_RDONLY);
    if (fd < 0) {
        return -1;
    }
    ssize_t length = read(fd, buffer, size - 1);
    close(fd);
    if (length <= 0) {
        return -1;
    }
    buffer[length] = '\0';
    while (length > 0 && (buffer[length - 1] == '\n' || buffer[length - 1] == ' ')) {
        buffer[--length] = '\0';
    }
    return 0;
}

// Reads the first sector of the block device the stick publishes.
static int check_block(void) {
    struct stat info;
    if (stat("/dev/sda", &info) != 0) {
        return 0;
    }

    int fd = open("/dev/sda", O_RDONLY);
    if (fd < 0) {
        return 0;
    }
    unsigned char sector[512];
    ssize_t got = read(fd, sector, sizeof(sector));
    close(fd);
    if (got != (ssize_t)sizeof(sector)) {
        printf("USBPROBE_BLOCK dev=/dev/sda read=%zd\n", got);
        return 0;
    }

    printf("USBPROBE_BLOCK dev=/dev/sda read=%zd magic=%02x%02x%02x%02x%02x%02x%02x%02x\n",
           got, sector[0], sector[1], sector[2], sector[3],
           sector[4], sector[5], sector[6], sector[7]);
    return 1;
}

// Returns the number of device entries printed.
static int scan_devices(void) {
    DIR *dir = opendir("/sys/bus/usb/devices");
    if (dir == NULL) {
        return 0;
    }

    int found = 0;
    struct dirent *entry;
    while ((entry = readdir(dir)) != NULL) {
        const char *name = entry->d_name;
        if (name[0] == '.' || strcmp(name, "usb1") == 0) {
            continue;
        }

        char vendor[32];
        char product[32];
        if (read_attr(name, "idVendor", vendor, sizeof(vendor)) != 0) {
            continue;
        }
        if (read_attr(name, "idProduct", product, sizeof(product)) != 0) {
            continue;
        }

        printf("USBPROBE_DEVICE name=%s vendor=%s product=%s\n", name, vendor, product);

        char config[32];
        if (read_attr(name, "bConfigurationValue", config, sizeof(config)) == 0) {
            char interfaces[32];
            if (read_attr(name, "bNumInterfaces", interfaces, sizeof(interfaces)) == 0) {
                printf("USBPROBE_CONFIG name=%s config=%s interfaces=%s\n",
                       name, config, interfaces);
            }
        }
        found++;
    }
    closedir(dir);
    return found;
}

int main(void) {
    setvbuf(stdout, NULL, _IONBF, 0);

    if (getpid() == 1) {
        mkdir("/dev", 0755);
        if (mount("devtmpfs", "/dev", "devtmpfs", 0, NULL) != 0 && errno != EBUSY) {
            printf("USBPROBE_FAIL mount-devtmpfs errno=%d\n", errno);
            return 1;
        }

        mkdir("/sys", 0755);
        if (mount("sysfs", "/sys", "sysfs", 0, NULL) != 0 && errno != EBUSY) {
            printf("USBPROBE_FAIL mount-sysfs errno=%d\n", errno);
            return 1;
        }
    }

    printf("USBPROBE_READY\n");

    // The gate hot-adds the stick after the controller is up; give it 90s.
    for (int attempt = 0; attempt < 450; attempt++) {
        if (scan_devices() > 0 && check_block()) {
            printf("USBPROBE_PASS\n");
            break;
        }
        struct timespec pause = { .tv_sec = 0, .tv_nsec = 200 * 1000 * 1000 };
        nanosleep(&pause, NULL);
    }

    sleep(3600);
    return 0;
}
