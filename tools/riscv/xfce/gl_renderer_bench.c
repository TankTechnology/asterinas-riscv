// SPDX-License-Identifier: MPL-2.0

// Identifies Mesa's active renderer and times a small OpenGL/X11 workload.
// Results are mirrored to the serial console for the QEMU acceptance harness.

#define GL_GLEXT_PROTOTYPES
#include <GL/gl.h>
#include <GL/glx.h>
#include <X11/Xlib.h>

#include <stdarg.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>

#define BENCH_WIDTH 320
#define BENCH_HEIGHT 240
#define BENCH_FRAMES 30

static FILE *serial;

static void report(const char *format, ...) {
    va_list stdout_args;
    va_start(stdout_args, format);
    vfprintf(stdout, format, stdout_args);
    va_end(stdout_args);
    fflush(stdout);

    if (serial != NULL) {
        va_list serial_args;
        va_start(serial_args, format);
        vfprintf(serial, format, serial_args);
        va_end(serial_args);
        fflush(serial);
    }
}

static double monotonic_ms(void) {
    struct timespec timestamp;
    clock_gettime(CLOCK_MONOTONIC, &timestamp);
    return timestamp.tv_sec * 1000.0 + timestamp.tv_nsec / 1000000.0;
}

static GLuint compile_shader(GLenum type, const char *source) {
    GLuint shader = glCreateShader(type);
    glShaderSource(shader, 1, &source, NULL);
    glCompileShader(shader);

    GLint compiled = GL_FALSE;
    glGetShaderiv(shader, GL_COMPILE_STATUS, &compiled);
    if (compiled == GL_TRUE) {
        return shader;
    }

    char log[1024] = {0};
    glGetShaderInfoLog(shader, sizeof(log), NULL, log);
    report("XFCE_GL_ERROR shader-compile %s\n", log);
    glDeleteShader(shader);
    return 0;
}

static GLuint create_program(void) {
    static const char vertex_source[] =
        "attribute vec2 position;\n"
        "void main() { gl_Position = vec4(position, 0.0, 1.0); }\n";
    static const char fragment_source[] =
        "uniform float phase;\n"
        "void main() {\n"
        "  vec2 p = gl_FragCoord.xy / vec2(320.0, 240.0);\n"
        "  float value = p.x * 0.73 + p.y * 1.19 + phase;\n"
        "  for (int i = 0; i < 16; ++i)\n"
        "    value = fract(value * 1.6180339 + sin(value + float(i)));\n"
        "  gl_FragColor = vec4(value, p.x, p.y, 1.0);\n"
        "}\n";

    GLuint vertex = compile_shader(GL_VERTEX_SHADER, vertex_source);
    GLuint fragment = compile_shader(GL_FRAGMENT_SHADER, fragment_source);
    if (vertex == 0 || fragment == 0) {
        glDeleteShader(vertex);
        glDeleteShader(fragment);
        return 0;
    }

    GLuint program = glCreateProgram();
    glAttachShader(program, vertex);
    glAttachShader(program, fragment);
    glBindAttribLocation(program, 0, "position");
    glLinkProgram(program);
    glDeleteShader(vertex);
    glDeleteShader(fragment);

    GLint linked = GL_FALSE;
    glGetProgramiv(program, GL_LINK_STATUS, &linked);
    if (linked == GL_TRUE) {
        return program;
    }

    char log[1024] = {0};
    glGetProgramInfoLog(program, sizeof(log), NULL, log);
    report("XFCE_GL_ERROR program-link %s\n", log);
    glDeleteProgram(program);
    return 0;
}

static bool draw_frame(Display *display, Window window, GLuint program,
                       GLint phase_location, unsigned int frame) {
    static const GLfloat vertices[] = {-1.0f, -1.0f, 3.0f, -1.0f, -1.0f, 3.0f};
    glViewport(0, 0, BENCH_WIDTH, BENCH_HEIGHT);
    glUseProgram(program);
    glUniform1f(phase_location, (GLfloat)frame * 0.03125f);
    glVertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE, 0, vertices);
    glEnableVertexAttribArray(0);
    glDrawArrays(GL_TRIANGLES, 0, 3);
    glXSwapBuffers(display, window);
    return glGetError() == GL_NO_ERROR;
}

int main(void) {
    serial = fopen("/dev/ttyS0", "w");
    report("XFCE_GL_BENCH_START\n");

    Display *x_display = XOpenDisplay(NULL);
    if (x_display == NULL) {
        report("XFCE_GL_ERROR x-open-display\n");
        return 1;
    }

    static int visual_attributes[] = {
        GLX_RGBA,
        GLX_DOUBLEBUFFER,
        GLX_RED_SIZE, 8,
        GLX_GREEN_SIZE, 8,
        GLX_BLUE_SIZE, 8,
        None,
    };
    int screen = DefaultScreen(x_display);
    XVisualInfo *visual = glXChooseVisual(x_display, screen, visual_attributes);
    if (visual == NULL) {
        report("XFCE_GL_ERROR glx-choose-visual\n");
        XCloseDisplay(x_display);
        return 1;
    }

    Colormap colormap = XCreateColormap(
        x_display, RootWindow(x_display, screen), visual->visual, AllocNone);
    XSetWindowAttributes window_attributes = {
        .colormap = colormap,
        .border_pixel = 0,
    };
    Window window = XCreateWindow(
        x_display, RootWindow(x_display, screen), 32, 32, BENCH_WIDTH,
        BENCH_HEIGHT, 0, visual->depth, InputOutput, visual->visual,
        CWBorderPixel | CWColormap, &window_attributes);
    XStoreName(x_display, window, "Asterinas GPU benchmark");
    XMapWindow(x_display, window);
    XSync(x_display, False);

    GLXContext context = glXCreateContext(x_display, visual, NULL, True);
    if (context == NULL || glXMakeCurrent(x_display, window, context) != True) {
        report("XFCE_GL_ERROR glx-context\n");
        if (context != NULL) {
            glXDestroyContext(x_display, context);
        }
        XDestroyWindow(x_display, window);
        XFreeColormap(x_display, colormap);
        XFree(visual);
        XCloseDisplay(x_display);
        return 1;
    }

    const char *renderer = (const char *)glGetString(GL_RENDERER);
    const char *version = (const char *)glGetString(GL_VERSION);
    report("XFCE_GL_DIRECT %s\n",
           glXIsDirect(x_display, context) == True ? "yes" : "no");
    report("XFCE_GL_RENDERER %s\n", renderer != NULL ? renderer : "unknown");
    report("XFCE_GL_VERSION %s\n", version != NULL ? version : "unknown");

    GLuint program = create_program();
    if (program == 0) {
        return 1;
    }
    GLint phase_location = glGetUniformLocation(program, "phase");

    for (unsigned int frame = 0; frame < 3; ++frame) {
        if (!draw_frame(x_display, window, program, phase_location, frame)) {
            report("XFCE_GL_ERROR warmup-frame=%u\n", frame);
            return 1;
        }
    }
    glFinish();

    double start_ms = monotonic_ms();
    for (unsigned int frame = 0; frame < BENCH_FRAMES; ++frame) {
        if (!draw_frame(x_display, window, program, phase_location, frame + 3)) {
            report("XFCE_GL_ERROR bench-frame=%u\n", frame);
            return 1;
        }
    }
    glFinish();
    double elapsed_ms = monotonic_ms() - start_ms;

    GLubyte pixel[4] = {0};
    glReadPixels(BENCH_WIDTH / 2, BENCH_HEIGHT / 2, 1, 1, GL_RGBA,
                 GL_UNSIGNED_BYTE, pixel);
    double fps = BENCH_FRAMES * 1000.0 / elapsed_ms;
    report("XFCE_GL_PIXEL %u,%u,%u,%u\n", pixel[0], pixel[1], pixel[2], pixel[3]);
    report("XFCE_GL_BENCH frames=%d elapsed_ms=%.3f fps=%.3f\n",
           BENCH_FRAMES, elapsed_ms, fps);
    report("XFCE_GL_BENCH_PASS\n");

    glDeleteProgram(program);
    glXMakeCurrent(x_display, None, NULL);
    glXDestroyContext(x_display, context);
    XDestroyWindow(x_display, window);
    XFreeColormap(x_display, colormap);
    XFree(visual);
    XCloseDisplay(x_display);
    if (serial != NULL) {
        fclose(serial);
    }
    return 0;
}
