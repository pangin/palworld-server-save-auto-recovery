#!/bin/bash

# DO NOT CHANGE
SAV_FILENAME_EXTENSION=".sav"
JSON_FILENAME_EXTENSION=".json"
SAVE_CORRUPT_DETECT_VALUE="None.PalWorldPlayerSaveGame"
# ---------------------------------------------------- #

# MODIFY THIS
PALWORLD_SAVEFILE_DIR=""
PALWORLD_SAVE_TOOLS_DIR=""
PALWORLD_SAVE_TOOLS_WORKSPACE_DIR=""


# SCRIPT START
mkdir $PALWORLD_SAVE_TOOLS_WORKSPACE_DIR

save_file_list=($(find "$PALWORLD_SAVEFILE_DIR" -type f -name "*$SAV_FILENAME_EXTENSION"))

for save_file in "${save_file_list[@]}"; do
        sav_file_id=$(basename $save_file $SAV_FILENAME_EXTENSION)

        python3 $PALWORLD_SAVE_TOOLS_DIR/convert.py $save_file --output $PALWORLD_SAVE_TOOLS_WORKSPACE_DIR/$save_file_id.json
done

json_save_file_list=($(find "$PALWORLD_SAVE_TOOLS_WORKSPACE_DIR" -type f -name "*$JSON_FILENAME_EXTENSION"))

for json_file in "${json_save_file_list[@]}"; do
        save_game_class_name=$(cat $json_file | jq -r '.header.save_game_class_name')

        echo $save_game_class_name
        echo $SAVE_CORRUPT_DETECT_VALUE

        if [ $save_game_class_name == $SAVE_CORRUPT_DETECT_VALUE ]; then
                json_file_id=$(basename $json_file $JSON_FILENAME_EXTENSION)

                jq '.header.save_game_class_name = "/Script/Pal.PalWorldPlayerSaveGame"' $json_file > $json_file.tmp
                mv $json_file.tmp $json_file
                python3 $PALWORLD_SAVE_TOOLS_DIR/convert.py $json_file --output $PALWORLD_SAVEFILE_DIR/$json_file_id.sav
        fi
done

rm -rf $PALWORLD_SAVE_TOOLS_WORKSPACE_DIR