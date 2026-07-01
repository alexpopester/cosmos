# encoding: ascii-8bit

# Delegate to the core openc3 spec_helper so we get mock_redis, setup_system,
# kill_leftover_threads and the full RSpec configuration without duplicating it.
openc3_spec = File.expand_path('../../openc3/spec', __dir__)
$LOAD_PATH.unshift(openc3_spec) unless $LOAD_PATH.include?(openc3_spec)

require 'spec_helper'
