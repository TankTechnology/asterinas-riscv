#include <stdio.h>
#include <dirent.h>
#include <sys/stat.h>
#include <sys/sysmacros.h>
#include <unistd.h>

int main(void) {
    printf("M16 VT verification init\n");

    /* List /dev */
    DIR *d = opendir("/dev");
    if (d) {
        struct dirent *ent;
        printf("M16_DEV_LIST: [");
        int first = 1;
        while ((ent = readdir(d)) != NULL) {
            if (!first) printf(", ");
            printf("%s", ent->d_name);
            first = 0;
        }
        printf("]\n");
        closedir(d);
    } else {
        printf("M16_DEV_READ_FAIL\n");
    }

    /* Check tty1-tty10 */
    char path[64];
    for (int n = 1; n <= 10; n++) {
        snprintf(path, sizeof(path), "/dev/tty%d", n);
        struct stat st;
        if (stat(path, &st) == 0) {
            printf("M16_VT_OK: %s char=%d maj=%u min=%u\n", path,
                   (int)S_ISCHR(st.st_mode), major(st.st_rdev), minor(st.st_rdev));
        } else {
            printf("M16_VT_MISS: %s\n", path);
        }
    }

    /* Check /dev/tty0 */
    struct stat st;
    if (stat("/dev/tty0", &st) == 0) {
        printf("M16_TTY0_OK: char=%d maj=%u min=%u\n",
               (int)S_ISCHR(st.st_mode), major(st.st_rdev), minor(st.st_rdev));
    } else {
        printf("M16_TTY0_MISS\n");
    }

    /* Check /dev/dri/card0 */
    if (stat("/dev/dri/card0", &st) == 0) {
        printf("M16_DRM_OK: /dev/dri/card0 char=%d maj=%u min=%u\n",
               (int)S_ISCHR(st.st_mode), major(st.st_rdev), minor(st.st_rdev));
    } else {
        printf("M16_DRM_MISS: /dev/dri/card0\n");
    }

    /* Check /dev/dri/renderD128 */
    if (stat("/dev/dri/renderD128", &st) == 0) {
        printf("M16_RENDER_OK: /dev/dri/renderD128\n");
    } else {
        printf("M16_RENDER_MISS: /dev/dri/renderD128 not found\n");
    }

    printf("M16_VERIFY_DONE\n");
    return 0;
}
