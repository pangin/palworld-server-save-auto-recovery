# palworld-server-save-auto-recovery

Script for palworld server save auto recovery   

**For now support Players save data only.**   

**Excecute script with root**

**Stop Palworld server and then use this script**

<br/>
<br/>

## How to use?

1. Clone this project or just download recovery.sh file to anywhere you want

2. Give execute permission to recovery.sh file

3. <a href="https://jqlang.github.io/jq/download/">install jq</a>


4. install python3

5. Clone <a href="https://github.com/cheahjs/palworld-save-tools/tree/main">palworld-save-tools</a> in to any directory

6. modify script variables

7. Code your server restart script to run this script after stopping the server

<br/>
<br/>

## Variables
|Variable                         |INFO                                      |EXAMPLE|
|---------------------------------|------------------------------------------|-------|
|PALWORLD_SAVEFILE_DIR            |Directory of Palworld user save file      |/home/user/Pal/Saved/SaveGames/.../Players|
|PALWORLD_SAVE_TOOLS_DIR          |Directory of palworld-save-tools          |/home/user/palworld-save-tools|
|PALWORLD_SAVE_TOOLS_WORKSPACE_DIR|Directory of palworld-save-tools workspace|/home/user/tmp/palworld-save-tools|
