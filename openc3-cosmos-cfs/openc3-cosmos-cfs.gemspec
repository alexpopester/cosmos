# encoding: ascii-8bit

Gem::Specification.new do |s|
  s.name = 'openc3-cosmos-cfs'
  s.summary = 'NASA cFS as a COSMOS target, commanded through a contact gate'
  s.description = <<-EOF
    Command and telemetry definitions for NASA's core Flight System (executive services,
    events, sample app, lab apps), reached through a gate that only passes traffic while
    a ground station can see the satellite cFS flies. For testing contact automation.
  EOF
  s.licenses = 'MIT'
  s.authors = ['Anonymous']
  s.email = ['name@domain.com']
  s.homepage = 'https://github.com/OpenC3/cosmos'
  s.platform = Gem::Platform::RUBY
  s.required_ruby_version = '>= 3.0'
  s.version = ENV['VERSION'] ? ENV['VERSION'].dup : "0.0.0.#{Time.now.strftime('%Y%m%d%H%M%S')}"
  s.files = Dir.glob("{targets,lib}/**/*") + %w(Rakefile README.md CHANGELOG.md plugin.txt)
end
