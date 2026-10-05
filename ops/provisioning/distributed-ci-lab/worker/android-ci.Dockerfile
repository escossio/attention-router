FROM eclipse-temurin:17-jdk-jammy
ENV ANDROID_HOME=/opt/android-sdk \
    ANDROID_SDK_ROOT=/opt/android-sdk \
    ANDROID_USER_HOME=/tmp/andy-android-user \
    JAVA_TOOL_OPTIONS=-Duser.home=/tmp/andy-home
COPY --chown=1000:1000 . /opt/android-sdk
RUN test -x /opt/android-sdk/platform-tools/adb \
 && test -d /opt/android-sdk/platforms/android-37.0 \
 && test -d /opt/android-sdk/build-tools/36.0.0
