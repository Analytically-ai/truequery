# Demo: /truequery review in a real Claude Code session on the demo shop.
# Setup (outside the recording):
#   - copy plugin/ to /tmp/truequery/plugin (a neutral path, since it shows
#     on screen) and export TQ_PLUGIN=/tmp/truequery/plugin
#   - the demo shop at /tmp/shop: examples/shop after ./generate.sh and
#     /truequery init (DATA.md and contracts/ in place)
# Record: vhs docs/demo-claude-code.tape (writes demo-claude-code.gif; real
# time, at 3x playback). Then cut it: typing at normal speed, the tool calls
# at 3x, a 6 s hold on the first screen of the answer (the checker receipt,
# the bottom line and the first blockers), the rest of the answer, then an
# 8 s hold on the recommendation; 4 fps, 48 colours:
#   ffmpeg -i demo-claude-code.gif -filter_complex "[0:v]trim=0:3.6,setpts=PTS-STARTPTS[a];[0:v]trim=3.6:ANSWER,setpts=(PTS-STARTPTS)/3[b];[0:v]trim=ANSWER:ANSWER+0.2,setpts=PTS-STARTPTS,tpad=stop_mode=clone:stop_duration=6[c];[0:v]trim=ANSWER+0.2:DONE,setpts=PTS-STARTPTS[d];[0:v]trim=DONE:DONE+8,setpts=PTS-STARTPTS[e];[a][b][c][d][e]concat=n=5:v=1[v];[v]fps=4,split[x][y];[x]palettegen=max_colors=48:stats_mode=diff[p];[y][p]paletteuse=dither=none:diff_mode=rectangle" docs/demo-claude-code.gif
#   (ANSWER = the frame where the final answer starts with the receipt;
#   DONE = when the answer is complete: ffmpeg's freezedetect finds it.)
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
