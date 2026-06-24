# Script Runner test script
cmd("HTTPJSONDEMO EXAMPLE")
wait_check("HTTPJSONDEMO STATUS BOOL == 'FALSE'", 5)
