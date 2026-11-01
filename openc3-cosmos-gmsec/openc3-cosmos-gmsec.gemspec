spec = Gem::Specification.new do |s|
  s.name = 'openc3-cosmos-gmsec'
  s.summary = 'GMSEC telemetry interface for OpenC3 COSMOS'
  s.description = 'Subscribes to GMSEC API 4.9.1 subjects and feeds the raw packet bytes carried in COMPAT C2 / C2MS messages into the COSMOS telemetry pipeline.'
  s.authors = ['Alex Pope']
  s.email = ['alexpopester@gmail.com']
  s.homepage = 'https://github.com/OpenC3/cosmos'
  s.platform = Gem::Platform::RUBY

  if ENV['VERSION']
    s.version = ENV['VERSION'].dup
  else
    s.version = '0.0.0' + ".#{Time.now.strftime('%Y%m%d%H%M%S')}"
  end
  s.license = 'MIT'
  s.files = Dir.glob('{targets,lib,tools}/**/*').reject { |f| f.include?('__pycache__') } + %w(Rakefile README.md plugin.txt)
end
