// Configuración de módulos JUCE para VocalEngine (se incluye antes de cada módulo).
#pragma once

#define JUCE_GLOBAL_MODULE_SETTINGS_INCLUDED 1
#define JUCE_STANDALONE_APPLICATION 1
#define JucePlugin_Build_Standalone 0

// Audio
#define JUCE_ASIO 1
#define JUCE_WASAPI 1
#define JUCE_DIRECTSOUND 0
#define JUCE_USE_WINRT_MIDI 0

// Hosting de plugins
#define JUCE_PLUGINHOST_VST3 1
#define JUCE_PLUGINHOST_VST 0
#define JUCE_PLUGINHOST_LV2 0
#define JUCE_PLUGINHOST_ARA 0
#define JUCE_PLUGINHOST_AU 0

// Cosas que no usamos
#define JUCE_USE_CURL 0
#define JUCE_WEB_BROWSER 0
#define JUCE_USE_CAMERA 0
#define JUCE_DISPLAY_SPLASH_SCREEN 0
#define JUCE_REPORT_APP_USAGE 0
#define JUCE_USE_MP3AUDIOFORMAT 0
#define JUCE_USE_FLAC 0
#define JUCE_USE_OGGVORBIS 0
#define JUCE_USE_WINDOWS_MEDIA_FORMAT 0
#define JUCE_CHECK_MEMORY_LEAKS 0

#define JUCE_MODULE_AVAILABLE_juce_core 1
#define JUCE_MODULE_AVAILABLE_juce_events 1
#define JUCE_MODULE_AVAILABLE_juce_data_structures 1
#define JUCE_MODULE_AVAILABLE_juce_graphics 1
#define JUCE_MODULE_AVAILABLE_juce_gui_basics 1
#define JUCE_MODULE_AVAILABLE_juce_gui_extra 1
#define JUCE_MODULE_AVAILABLE_juce_audio_basics 1
#define JUCE_MODULE_AVAILABLE_juce_audio_devices 1
#define JUCE_MODULE_AVAILABLE_juce_audio_formats 1
#define JUCE_MODULE_AVAILABLE_juce_audio_processors 1
#define JUCE_MODULE_AVAILABLE_juce_audio_utils 1
#define JUCE_MODULE_AVAILABLE_juce_dsp 1
