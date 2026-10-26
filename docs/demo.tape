# Demo: detect silently-wrong SQL, then review it with the receipt.
# Render with: vhs docs/demo.tape  (writes docs/demo.gif)
Output docs/demo.gif
Set Shell "bash"
Set FontSize 15
Set Width 900
Set Height 520
Set Padding 10

Hide
Type "cd $ROOT && clear" Enter
Show

Type "# silently-wrong SQL: it runs, looks right, returns the wrong answer" Enter
Sleep 2s
Type "truequery detect samples/bad_orders.sql" Enter
Sleep 9s

Type "# review adds the receipt: what was checked, what was not" Enter
Sleep 2s
Type "truequery review samples/bad_orders.sql" Enter
Sleep 10s
