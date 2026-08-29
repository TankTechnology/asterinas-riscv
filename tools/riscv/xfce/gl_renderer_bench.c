// SPDX-License-Identifier: MPL-2.0

// Identifies Mesa's active renderer and times a small OpenGL/X11 workload.
// The systemd service directs stdout to the serial console for the QEMU
// acceptance harness.

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
#define VALIDATION_RED 32
#define VALIDATION_GREEN 128
#define VALIDATION_BLUE 223
#define VALIDATION_ALPHA 255
#define VALIDATION_TOLERANCE 2

static void report(const char *format, ...) {
    va_list args;
    va_start(args, format);
    vfprintf(stdout, format, args);
    va_end(args);
    fflush(stdout);
}

static double monotonic_ms(void) {
    struct timespec timestamp;
    clock_gettime(CLOCK_MONOTONIC, &timestamp);
    return timestamp.tv_sec * 1000.0 + timestamp.tv_nsec / 1000000.0;
}

static double process_cpu_ms(void) {
    struct timespec timestamp;
    clock_gettime(CLOCK_PROCESS_CPUTIME_ID, &timestamp);
    return timestamp.tv_sec * 1000.0 + timestamp.tv_nsec / 1000000.0;
}

static int compare_doubles(const void *left, const void *right) {
    double left_value = *(const double *)left;
    double right_value = *(const double *)right;
    return (left_value > right_value) - (left_value < right_value);
}

static double percentile_ms(const double *sorted_frame_ms,
                            unsigned int percentile) {
    unsigned int rank =
        (percentile * BENCH_FRAMES + 99) / 100;
    return sorted_frame_ms[rank - 1];
}

static bool component_matches(GLubyte actual, unsigned int expected) {
    unsigned int actual_value = actual;
    return actual_value + VALIDATION_TOLERANCE >= expected &&
           actual_value <= expected + VALIDATION_TOLERANCE;
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

    double frame_ms[BENCH_FRAMES];
    double start_ms = monotonic_ms();
    double start_cpu_ms = process_cpu_ms();
    for (unsigned int frame = 0; frame < BENCH_FRAMES; ++frame) {
        double frame_start_ms = monotonic_ms();
        if (!draw_frame(x_display, window, program, phase_location, frame + 3)) {
            report("XFCE_GL_ERROR bench-frame=%u\n", frame);
            return 1;
        }
        // Complete every submitted frame before timing the next one. A single
        // final `glFinish` measures queueing throughput rather than the
        // presentation latency experienced by an interactive client.
        glFinish();
        frame_ms[frame] = monotonic_ms() - frame_start_ms;
    }
    double elapsed_ms = monotonic_ms() - start_ms;
    double cpu_ms = process_cpu_ms() - start_cpu_ms;

    double sorted_frame_ms[BENCH_FRAMES];
    double frame_total_ms = 0.0;
    for (unsigned int frame = 0; frame < BENCH_FRAMES; ++frame) {
        sorted_frame_ms[frame] = frame_ms[frame];
        frame_total_ms += frame_ms[frame];
    }
    qsort(sorted_frame_ms, BENCH_FRAMES, sizeof(sorted_frame_ms[0]),
          compare_doubles);

    glDrawBuffer(GL_BACK);
    glClearColor(0.125f, 0.5f, 0.875f, 1.0f);
    glClear(GL_COLOR_BUFFER_BIT);
    glFinish();
    glReadBuffer(GL_BACK);
    GLubyte pixel[4] = {0};
    glReadPixels(BENCH_WIDTH / 2, BENCH_HEIGHT / 2, 1, 1, GL_RGBA,
                 GL_UNSIGNED_BYTE, pixel);
    bool pixel_is_valid = glGetError() == GL_NO_ERROR &&
                          component_matches(pixel[0], VALIDATION_RED) &&
                          component_matches(pixel[1], VALIDATION_GREEN) &&
                          component_matches(pixel[2], VALIDATION_BLUE) &&
                          component_matches(pixel[3], VALIDATION_ALPHA);
    double fps = BENCH_FRAMES * 1000.0 / elapsed_ms;
    report("XFCE_GL_PIXEL %u,%u,%u,%u\n", pixel[0], pixel[1], pixel[2], pixel[3]);
    report("XFCE_GL_BENCH frames=%d elapsed_ms=%.3f fps=%.3f\n",
           BENCH_FRAMES, elapsed_ms, fps);
    for (unsigned int frame = 0; frame < BENCH_FRAMES; ++frame) {
        report("XFCE_GL_FRAME index=%u elapsed_ms=%.3f\n", frame,
               frame_ms[frame]);
    }
    report("XFCE_GL_FRAME_TIMES frames=%d mean_ms=%.3f p50_ms=%.3f "
           "p95_ms=%.3f p99_ms=%.3f max_ms=%.3f cpu_ms=%.3f "
           "cpu_ms_per_frame=%.3f\n",
           BENCH_FRAMES, frame_total_ms / BENCH_FRAMES,
           percentile_ms(sorted_frame_ms, 50),
           percentile_ms(sorted_frame_ms, 95),
           percentile_ms(sorted_frame_ms, 99),
           sorted_frame_ms[BENCH_FRAMES - 1], cpu_ms,
           cpu_ms / BENCH_FRAMES);
    if (!pixel_is_valid) {
        report("XFCE_GL_ERROR validation-pixel\n");
        return 1;
    }
    report("XFCE_GL_BENCH_PASS\n");

    glDeleteProgram(program);
    glXMakeCurrent(x_display, None, NULL);
    glXDestroyContext(x_display, context);
    XDestroyWindow(x_display, window);
    XFreeColormap(x_display, colormap);
    XFree(visual);
    XCloseDisplay(x_display);
    return 0;
}
