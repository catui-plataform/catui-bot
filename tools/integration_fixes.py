"""Known integration defects, carried into the saved source patch and receipt."""
from pathlib import Path

EDEN_INVALID_CONTENT_GUARD = '''    // Reject unsupported content before allocating the emulation/GPU system.
    // Invalid NSP/XCI data must be a normal load error, including in headless hosts.
    auto preflight_vfs = std::make_shared<FileSys::RealVfsFilesystem>();
    const auto preflight_file = preflight_vfs->OpenFile(filepath, FileSys::OpenMode::Read);
    const auto preflight_type = preflight_file ? Loader::IdentifyFile(preflight_file)
                                              : Loader::FileType::Error;
    if (preflight_type == Loader::FileType::Error || preflight_type == Loader::FileType::Unknown) {
        LOG_ERROR(Frontend, "Unsupported or invalid content: {}", filepath);
        return false;
    }

'''


def apply_known_fixes(core, work):
    if core == "ppsspp":
        path = Path(work) / "libretro/libretro.cpp"
        text = path.read_text(encoding="utf-8")
        updated = text.replace(
            "bool retro_load_game(const struct retro_game_info *game) {\n   retro_pixel_format",
            "bool retro_load_game(const struct retro_game_info *game) {\n"
            "   if (!game || !game->path || !game->path[0])\n      return false;\n"
            "   retro_pixel_format", 1)
        if "   if (!ctx)\n      return false;" not in updated:
            updated = updated.replace(
                "ctx       = LibretroGraphicsContext::CreateGraphicsContext();\n",
                "ctx       = LibretroGraphicsContext::CreateGraphicsContext();\n"
                "   if (!ctx)\n      return false;\n", 1)
        if updated == text:
            return []
        path.write_text(updated, encoding="utf-8", newline="\n")
        return ["libretro/libretro.cpp"]
    if core == "flycast":
        path = Path(work) / "CMakeLists.txt"
        text = path.read_text(encoding="utf-8")
        updated = text.replace('if(NOT CMAKE_SYSTEM_NAME STREQUAL "OpenBSD")',
                               'if(NOT MSVC AND NOT CMAKE_SYSTEM_NAME STREQUAL "OpenBSD")')
        if 'if(NOT MSVC)\nset_target_properties' not in updated:
            updated = updated.replace('set_target_properties(${PROJECT_NAME} PROPERTIES LINK_FLAGS_RELEASE -s)',
                                      'if(NOT MSVC)\nset_target_properties(${PROJECT_NAME} PROPERTIES LINK_FLAGS_RELEASE -s)\nendif()')
        if updated == text:
            return []
        path.write_text(updated, encoding="utf-8", newline="\n")
        return ["CMakeLists.txt"]
    if core != "eden":
        return []
    path = Path(work) / "src/eden_libretro/libretro_core.cpp"
    text = path.read_text(encoding="utf-8")
    if "const auto preflight_type = preflight_file ? Loader::IdentifyFile(preflight_file)" in text:
        return []
    signature = "bool LibretroSession::LoadGame(const char* filepath) {"
    start = text.index(signature)
    anchor = "    if (is_game_loaded) {"
    insertion = text.index(anchor, start)
    text = text[:insertion] + EDEN_INVALID_CONTENT_GUARD + text[insertion:]
    include = '#include "core/loader/loader.h"'
    if include not in text:
        location = text.index("#include ")
        text = text[:location] + include + "\n" + text[location:]
    path.write_text(text, encoding="utf-8", newline="\n")
    return [path.relative_to(work).as_posix()]
