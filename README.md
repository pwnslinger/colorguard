## Colorguard

Detect and exploit leaks of the flag page given an input.
Makes stdin entirely concrete with the only symbolic data being the flag page.
For most binaries this should allow us to execute almost entirely in Unicorn

```python
>>> cg = colorguard.Colorguard("../binaries/tests/i386/simple_leak", "deadbeef")
>>> pov = cg.attempt_exploit() # if a leak occurs exploit it
>>> pov # these POVs are the same as the POVs generated by Rex
<colorguard.pov.ColorguardType2Exploit at 0x7f7d9a2c2610>
>>> pov.test_binary() # being Rex POVs they can also be run against a simulation of the CGC architecture
True
>>> pov.dump_c('leak.c') # they can be dumped just like Rex POVs too
>>> pov.dump_binary('leak.pov')
```

`pip install .` or `python setup.py install` should install it correctly.
