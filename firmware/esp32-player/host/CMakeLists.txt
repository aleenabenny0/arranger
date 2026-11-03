# Host-side tests for the firmware's plain-C parts: the frame protocol and the
# note logic. They compile the same files the firmware uses, with Unity from
# c/midi/tests/unity, and run under CTest with no ESP-IDF installed.
cmake_minimum_required(VERSION 3.16)
project(arranger_player_host C)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)
set(CMAKE_C_EXTENSIONS OFF)

option(FIRMWARE_WERROR "Treat compiler warnings as errors" ON)
option(FIRMWARE_SANITIZE "Build with AddressSanitizer and UndefinedBehaviorSanitizer (GCC or Clang)" OFF)

set(FIRMWARE ${CMAKE_CURRENT_SOURCE_DIR}/..)
set(UNITY ${CMAKE_CURRENT_SOURCE_DIR}/../../../c/midi/tests/unity)

if(MSVC)
  set(FIRMWARE_WARNINGS /W4)
  if(FIRMWARE_WERROR)
    list(APPEND FIRMWARE_WARNINGS /WX)
  endif()
else()
  set(FIRMWARE_WARNINGS -Wall -Wextra -Wpedantic -Wshadow -Wconversion -Wsign-conversion -Wstrict-prototypes)
  if(FIRMWARE_WERROR)
    list(APPEND FIRMWARE_WARNINGS -Werror)
  endif()
endif()

if(FIRMWARE_SANITIZE AND NOT MSVC)
  add_compile_options(-fsanitize=address,undefined -fno-omit-frame-pointer -fno-sanitize-recover=all)
  add_link_options(-fsanitize=address,undefined)
endif()

add_library(unity STATIC ${UNITY}/unity.c)
target_include_directories(unity PUBLIC ${UNITY})
target_compile_definitions(unity PUBLIC UNITY_INCLUDE_DOUBLE)

add_library(player STATIC ${FIRMWARE}/components/frameproto/frameproto.c ${FIRMWARE}/components/voice/voice.c)
target_include_directories(player PUBLIC ${FIRMWARE}/components/frameproto ${FIRMWARE}/components/voice)
target_compile_options(player PRIVATE ${FIRMWARE_WARNINGS})

enable_testing()

add_executable(test_frameproto test_frameproto.c)
target_link_libraries(test_frameproto PRIVATE player unity)
target_compile_options(test_frameproto PRIVATE ${FIRMWARE_WARNINGS})
add_test(NAME frameproto COMMAND test_frameproto)

add_executable(test_voice test_voice.c)
target_link_libraries(test_voice PRIVATE player unity m)
target_compile_options(test_voice PRIVATE ${FIRMWARE_WARNINGS})
add_test(NAME voice COMMAND test_voice)
