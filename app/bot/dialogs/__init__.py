"""aiogram-dialog flows. Migration target — wizard'ы и сложные multi-step UI.

Каждый dialog экспортирует `Dialog`-объект из своего модуля; общий dispatcher
подключает их через `setup_dialogs(dp)` в [app/bot/bot.py](../bot.py).
"""
from app.bot.dialogs.dev_broadcast import dev_broadcast_dialog
from app.bot.dialogs.edit_group import edit_group_dialog
from app.bot.dialogs.misc_dialogs import (
    add_teacher_preset_dialog,
    add_venue_dialog,
    broadcast_dialog,
    create_school_dialog,
    edit_name_dialog,
    rename_school_dialog,
)
from app.bot.dialogs.new_group import new_group_dialog
from app.bot.dialogs.states import (
    AddTeacherPresetSG,
    AddVenueSG,
    BroadcastSG,
    CreateSchoolSG,
    DevBroadcastSG,
    EditGroupSG,
    EditNameSG,
    NewGroupSG,
    RenameSchoolSG,
)

__all__ = [
    "AddTeacherPresetSG",
    "AddVenueSG",
    "BroadcastSG",
    "CreateSchoolSG",
    "DevBroadcastSG",
    "EditGroupSG",
    "EditNameSG",
    "NewGroupSG",
    "RenameSchoolSG",
    "add_teacher_preset_dialog",
    "add_venue_dialog",
    "broadcast_dialog",
    "create_school_dialog",
    "dev_broadcast_dialog",
    "edit_group_dialog",
    "edit_name_dialog",
    "new_group_dialog",
    "rename_school_dialog",
]
