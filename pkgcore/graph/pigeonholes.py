# Copyright: 2006 Brian Harring <ferringb@gmail.com>
# License: GPL2

from pkgcore.restrictions import restriction
from pkgcore.util.compatibility import any

# lil too getter/setter like for my tastes...

class PigeonHoledSlots(object):
	"""class for tracking slotting to a specific atom/obj key
	no atoms present, just prevents conflicts of obj.key; atom present, assumes
	it's a blocker and ensures no obj matches the atom for that key
	"""

	def __init__(self):
		self.slot_dict = {}
		
	def fill_slotting(self, obj):
		"""try to insert obj in, returning any conflicting objs (empty list if inserted successfully)"""
		key = obj.key
		l = []
		for x in self.slot_dict.setdefault(key, []):
			if isinstance(x, restriction.base):
				if x.match(obj):
					# no go.  blocker.
					l.append(x)
			else:
				if x.slot == obj.slot:
					if x == obj:
						# exit,with a sanity check.
						for y in (z for z in self.slot_dict[key] if isinstance(z, restriction.base)):
							if y.match(x):
								import pdb;pdb.set_trace()
								raise Exception
						return []
					l.append(x)
		if not l:
			self.slot_dict[key].append(obj)
		return l
			
	def add_limiter(self, atom):
		"""add a limiter, returning any conflicting objs"""
		if not isinstance(atom, restriction.base):
			raise TypeError("atom must be a restriction.base derivative")
		# debug.
#		if any(atom is x for x in self.slot_dict.get(atom.key, [])):
#			raise KeyError("%s is already in %s: %s" % (atom, atom.key, self.slot_dict[atom.key]))

		l = []
		for x in self.slot_dict.setdefault(atom.key, []):
			if not isinstance(x, restriction.base) and atom.match(x):
				l.append(x)
		self.slot_dict[atom.key].append(atom)
		return l
		
	def remove_slotting(self, obj):
		key = obj.key
		# let the key error be thrown if they screwed up.
		l = [x for x in self.slot_dict[key] if x is not obj]
		if len(l) == len(self.slot_dict[key]):
			raise KeyError("obj %s isn't slotted" % obj)
		if l:
			self.slot_dict[key] = l
		else:
			del self.slot_dict[key]

