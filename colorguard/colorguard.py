import os
import tracer
import random
import claripy
from itertools import groupby
from operator import itemgetter
from .harvester import Harvester
from .pov import ColorguardExploit, ColorguardNaiveExploit
from rex.trace_additions import ChallRespInfo, ZenPlugin
from simuvex import s_options as so
from simuvex.plugins.symbolic_memory import SimSymbolicMemory
from simuvex.storage import SimFile

import logging

l = logging.getLogger("colorguard.ColorGuard")

class ColorGuard(object):
    """
    Detect leaks of the magic flag page data.
    Most logic is offloaded to the tracer.
    """

    def __init__(self, binary, payload, format_infos=None):
        """
        :param binary: path to the binary which is suspect of leaking
        :param payload: concrete input string to feed to the binary
        :param format_infos: a list of atoi FormatInfo objects that should be used when analyzing the crash
        """

        self.binary = binary
        self.payload = payload

        if not os.access(self.binary, os.X_OK):
            raise ValueError("\"%s\" binary does not exist or is not executable" % self.binary)

        # will be set by causes_leak
        self._leak_path = None

        remove_options = {so.SUPPORT_FLOATING_POINT}
        self._tracer = tracer.Tracer(binary, payload, preconstrain_input=False, remove_options=remove_options)
        ZenPlugin.prep_tracer(self._tracer)

        e_path = self._tracer.path_group.active[0]

        backing = SimSymbolicMemory(memory_id='file_colorguard')
        backing.set_state(e_path.state)
        backing.store(0, e_path.state.se.BVV(payload))

        e_path.state.posix.files[0] = SimFile('/dev/stdin', 'r', content=backing, size=len(payload))

        # will be overwritten by _concrete_difference if the input was filtered
        # this attributed is used exclusively for testing at the moment
        self._no_concrete_difference = False

        self.leak_ast = None

    def _concrete_leak_info(self, seed=None):

        if seed is None:
            seed = random.randint(0, 2**32)

        r1 = tracer.Runner(self.binary,
                input=self.payload,
                record_magic=True,
                record_stdout=True,
                seed=seed)

        return (r1.stdout, r1.magic)

    def _concrete_difference(self):
        """
        Does an input when ran concretely produce two separate outputs?
        If it causes a leak it should, but if the outputs differ
        it is not guaranteed there is a leak.

        :return: True if the there is a concrete difference
        """

        s1, _ = self._concrete_leak_info()
        s2, _ = self._concrete_leak_info()

        # mark a flag so we can test this method's effectiveness
        self._no_concrete_difference = s1 == s2

        return not self._no_concrete_difference

    def causes_dumb_leak(self):

        return self._concrete_difference()

    def _find_dumb_leaks(self):

        s1, m1 = self._concrete_leak_info()

        potential_leaks = [ ]
        for i in xrange(len(s1)):
            pchunk = s1[i:i+4]
            if len(pchunk) == 4 and pchunk in m1:
                potential_leaks.append(i)

        return (potential_leaks, s1)

    def attempt_dumb_pov(self):

        p1, stdout = self._find_dumb_leaks()
        p2, _ = self._find_dumb_leaks()

        leaks = list(set(p1).intersection(set(p2)))

        if leaks:
            leaked_bytes = range(leaks[0], leaks[0]+4)
            l.info("Found dumb leak which leaks bytes %s", leaked_bytes)

            return ColorguardNaiveExploit(self.binary, self.payload, len(stdout), leaked_bytes)
        else:
            l.debug("No dumb leak found")

    def causes_naive_leak(self):

        return self.causes_dumb_leak()

    def _find_naive_leaks(self, seed=None):
        """
        Naive implementation of colorguard which looks for concrete leaks of
        the flag page.
        """

        stdout, magic = self._concrete_leak_info(seed=seed)

        # byte indices where a leak might have occured
        potential_leaks = dict()
        for si, b in enumerate(stdout):
            try:
                indices = [i for i, x in enumerate(magic) if x == b]
                potential_leaks[si] = indices
            except ValueError:
                pass

        return (potential_leaks, stdout)

    def attempt_naive_pov(self):

        p1, stdout = self._find_naive_leaks()
        p2, _ = self._find_naive_leaks()

        leaked = dict()
        for si in p1:
            if si in p2:
                li = list(set(p2[si]).intersection(set(p1[si])))
                if len(li) > 0:
                    for lb in li:
                        leaked[lb] = si

        # find four contiguous
        consecutive_groups = [ ]
        for _, g in groupby(enumerate(sorted(leaked)), lambda (i,x):i-x):
            consecutive_groups.append(map(itemgetter(1), g))

        lgroups = filter(lambda x: len(x) >= 4, consecutive_groups)

        if len(lgroups):
            l.info("Found naive leak which leaks bytes %s", lgroups[0])
            leaked_bytes = [ ]
            for b in leaked:
                leaked_bytes.append(leaked[b])

            return ColorguardNaiveExploit(self.binary, self.payload, len(stdout), leaked_bytes)
        else:
            l.debug("No naive leak found")

    def causes_leak(self):

        if not self.causes_naive_leak():
            return False

        self._leak_path, _ = self._tracer.run()

        stdout = self._leak_path.state.posix.files[1]
        tmp_pos = stdout.read_pos
        stdout.pos = 0

        output = stdout.read_from(tmp_pos)

        for var in output.variables:
            if var.startswith("cgc-flag"):
                self.leak_ast = output
                return True

        return False

    def attempt_pov(self, enabled_chall_resp=False):

        assert self.leak_ast is not None, "must run causes_leak first or input must cause a leak"

        st = self._leak_path.state

        # switch to a composite solver
        self._tracer.remove_preconstraints(self._leak_path, simplify=False)

        # get the flag var
        flag_bytes = self._tracer.cgc_flag_bytes

        # remove constraints from the state which involve only the flagpage
        # this solves a problem with CROMU_00070, where the floating point
        # operations have to be done concretely and constrain the flagpage
        # to being a single value
        zen_cache_keys = set(x.cache_key for x in st.get_plugin("zen_plugin").zen_constraints)
        new_cons = [ ]
        for con in st.se.constraints:
            if con.cache_key in zen_cache_keys or not all(v.startswith("cgc-flag") for v in con.variables):
                new_cons.append(con)

        st.release_plugin('solver_engine')
        st.add_constraints(*new_cons)
        st.downsize()

        st.se.simplify()
        st.se._solver.result = None

        simplified = st.se.simplify(self.leak_ast)

        harvester = Harvester(simplified, st.copy(), flag_bytes)

        output_var = claripy.BVS('output_var', harvester.minimized_ast.size(), explicit_name=True) #pylint:disable=no-member

        st.add_constraints(harvester.minimized_ast == output_var)

        exploit = ColorguardExploit(self.binary, st,
                                    self.payload, harvester,
                                    simplified, output_var)

        # only want to try this once
        if not enabled_chall_resp:
            l.info('testing for challenge response')
            if self._challenge_response_exists(exploit):
                l.warning('challenge response detected')
                exploit = self._prep_challenge_response()

        return exploit

### CHALLENGE RESPONSE

    @staticmethod
    def _challenge_response_exists(exploit):

        return not any(exploit.test_binary(times=10, enable_randomness=True))

    def _prep_challenge_response(self, format_infos=None):

        # need to re-trace the binary with stdin symbolic

        remove_options = {so.SUPPORT_FLOATING_POINT}
        self._tracer = tracer.Tracer(self.binary, self.payload, remove_options=remove_options)
        ChallRespInfo.prep_tracer(self._tracer, format_infos)

        assert self.causes_leak(), "challenge did not cause leak when trying to recover challenge-response"

        return self.attempt_pov(enabled_chall_resp=True)
