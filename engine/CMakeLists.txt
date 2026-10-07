# Compilación alternativa en Windows con Visual Studio 2022 (opcional).
# El VocalEngine.exe incluido ya viene compilado; esto es solo para modificar el motor.
#   cmake -B build-msvc -G "Visual Studio 17 2022" && cmake --build build-msvc --config Release
cmake_minimum_required(VERSION 3.22)
project(VocalEngine VERSION 1.0)
set(CMAKE_CXX_STANDARD 17)
include(FetchContent)
FetchContent_Declare(juce GIT_REPOSITORY https://github.com/juce-framework/JUCE.git GIT_TAG 7.0.12 GIT_SHALLOW TRUE)
FetchContent_Declare(asiosdk GIT_REPOSITORY https://github.com/audiosdk/asio.git GIT_TAG main GIT_SHALLOW TRUE)
FetchContent_MakeAvailable(juce asiosdk)

juce_add_gui_app(VocalEngine PRODUCT_NAME "VocalEngine")
target_sources(VocalEngine PRIVATE src/Main.cpp src/Engine.cpp src/Recorder.cpp)
target_include_directories(VocalEngine PRIVATE src ${asiosdk_SOURCE_DIR}/common)
target_compile_definitions(VocalEngine PRIVATE
    JUCE_ASIO=1 JUCE_WASAPI=1 JUCE_DIRECTSOUND=0 JUCE_PLUGINHOST_VST3=1 JUCE_PLUGINHOST_VST=0
    JUCE_USE_CURL=0 JUCE_WEB_BROWSER=0 JUCE_DISPLAY_SPLASH_SCREEN=0 JUCE_REPORT_APP_USAGE=0
    JUCE_APPLICATION_NAME_STRING="VocalEngine" JUCE_APPLICATION_VERSION_STRING="1.0")
target_link_libraries(VocalEngine PRIVATE juce::juce_audio_utils juce::juce_audio_processors juce::juce_dsp
    juce::juce_recommended_config_flags juce::juce_recommended_warning_flags)
