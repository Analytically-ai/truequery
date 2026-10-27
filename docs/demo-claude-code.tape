# Demo: /truequery review in a real Claude Code session on the demo shop.
# Setup (outside the recording):
#   - copy plugin/ to /tmp/truequery/plugin (a neutral path, since it shows
#     on screen) and export TQ_PLUGIN=/tmp/truequery/plugin
#   - the demo shop at /tmp/shop: examples/shop after ./generate.sh and
#     /truequery init (DATA.md and contracts/ in place)
# Record: vhs docs/demo-claude-code.tape (writes demo-claude-code.gif; real
# time, at 3x playback). Then cut it: typing at normal speed, the tool calls
# at 2.5x, an 8 s hold on the final answer, 5 fps, 48 colours:
#   ffmpeg -i demo-claude-code.gif -filter_complex "[0:v]trim=0:3.6,setpts=PTS-STARTPTS[a];[0:v]trim=3.6:DONE,setpts=(PTS-STARTPTS)/2.5[b];[0:v]trim=DONE:DONE+8,setpts=PTS-STARTPTS[c];[a][b][c]concat=n=3:v=1[v];[v]fps=5,split[x][y];[x]palettegen=max_colors=48:stats_mode=diff[p];[y][p]paletteuse=dither=none:diff_mode=rectangle" docs/demo-claude-code.gif
#   (DONE = when the answer is complete: ffmpeg's freezedetect finds it.)
# The session is real, so each recording differs.
Output demo-claude-code.gif
Set Shell "bash"
Set FontSize 14
Set Width 1240
Set Height 860
Set Padding 12
Set PlaybackSpeed 3
Hide
Type "cd /tmp/shop" Enter
Type "clear; claude --plugin-dir $TQ_PLUGIN --model claude-opus-5-5" Enter
Sleep 6s
Down
Enter
Sleep 6s
Show
Sleep 2s
Type@60ms "/truequery review dbt_repo/models/marts/fct_orders.sql"
Sleep 1s
Enter
Sleep 150s
Sleep 15s
