spec = Gem::Specification.new do |s|
  s.name = 'openc3-cosmos-json-tlm'
  s.summary = 'REST/JSON telemetry ingest for OpenC3 COSMOS'
  s.description = 'Accepts telemetry as JSON over HTTP and feeds it through the normal COSMOS pipeline using existing packet definitions.'
  s.authors = ['Your Name']
  s.email = ['you@example.com']
  s.homepage = 'https://github.com/OpenC3/cosmos'
  s.platform = Gem::Platform::RUBY

  if ENV['VERSION']
    s.version = ENV['VERSION'].dup
  else
    s.version = '0.0.0' + ".#{Time.now.strftime('%Y%m%d%H%M%S')}"
  end
  s.license = 'MIT'
  s.files = Dir.glob('{targets,lib}/**/*').reject { |f| f.include?('__pycache__') } + %w(Rakefile README.md plugin.txt requirements.txt)
end
