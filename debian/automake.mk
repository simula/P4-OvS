EXTRA_DIST += \
	debian/changelog \
	debian/compat \
	debian/control \
	debian/copyright \
	debian/copyright.in \
	debian/dirs \
	debian/dkms.conf.in \
	debian/gbp.conf \
	debian/ifupdown.sh \
	debian/openvswitch-p4-common.dirs \
	debian/openvswitch-p4-common.install \
	debian/openvswitch-p4-doc.doc-base \
	debian/openvswitch-p4-doc.install \
	debian/openvswitch-p4-pki.dirs \
	debian/openvswitch-p4-pki.postinst \
	debian/openvswitch-p4-pki.postrm \
	debian/openvswitch-p4-source.dirs \
	debian/openvswitch-p4-source.install \
	debian/openvswitch-p4-switch.default \
	debian/openvswitch-p4-switch.dirs \
	debian/openvswitch-p4-switch-dpdk.postinst \
	debian/openvswitch-p4-switch-dpdk.prerm \
	debian/openvswitch-p4-switch-dpdk.README.Debian \
	debian/openvswitch-p4-switch.init \
	debian/openvswitch-p4-switch.install \
	debian/openvswitch-p4-switch.links \
	debian/openvswitch-p4-switch.logrotate \
	debian/openvswitch-p4-switch.ovsdb-server.service \
	debian/openvswitch-p4-switch.ovs-vswitchd.service \
	debian/openvswitch-p4-switch.postinst \
	debian/openvswitch-p4-switch.postrm \
	debian/openvswitch-p4-switch.prerm \
	debian/openvswitch-p4-switch.README.Debian \
	debian/openvswitch-p4-switch.service \
	debian/openvswitch-p4-testcontroller.default \
	debian/openvswitch-p4-testcontroller.dirs \
	debian/openvswitch-p4-testcontroller.init \
	debian/openvswitch-p4-testcontroller.install \
	debian/openvswitch-p4-testcontroller.postinst \
	debian/openvswitch-p4-testcontroller.postrm \
	debian/openvswitch-p4-testcontroller.README.Debian \
	debian/openvswitch-p4-test.install \
	debian/openvswitch-p4-vtep.default \
	debian/openvswitch-p4-vtep.dirs \
	debian/openvswitch-p4-vtep.init \
	debian/openvswitch-p4-vtep.install \
	debian/ovs-systemd-reload \
	debian/patches/series \
	debian/rules \
	debian/rules.modules \
	debian/source/format \
	debian/tests/control \
	debian/tests/dpdk \
	debian/tests/openflow.py \
	debian/tests/vanilla

check-debian-changelog-version:
	@DEB_VERSION=`echo '$(VERSION)' | sed 's/pre/~pre/'`;		     \
	if $(FGREP) '($(DEB_VERSION)' $(srcdir)/debian/changelog >/dev/null; \
	then								     \
	  :;								     \
	else								     \
	  echo "Update debian/changelog to mention version $(VERSION)";	     \
	  exit 1;							     \
	fi
ALL_LOCAL += check-debian-changelog-version
DIST_HOOKS += check-debian-changelog-version

$(srcdir)/debian/copyright: AUTHORS.rst debian/copyright.in
	$(AM_V_GEN) \
	{ sed -n -e '/%AUTHORS%/q' -e p < $(srcdir)/debian/copyright.in;   \
	  sed '34,/^$$/d' $(srcdir)/AUTHORS.rst  |				   \
		sed -n -e '/^$$/q' -e 's/^/  /p';			   \
	  sed -e '34,/%AUTHORS%/d' $(srcdir)/debian/copyright.in;	   \
	} > $@

DISTCLEANFILES += debian/copyright
