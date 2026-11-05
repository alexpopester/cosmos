# encoding: ascii-8bit

Gem::Specification.new do |s|
  s.name = 'openc3-cosmos-satnogs'
  s.summary = 'SatNOGS: real satellite telemetry from the SatNOGS ground station network'
  s.description = <<-EOF
    Polls the SatNOGS network for frames that volunteer ground stations received
    from real satellites, decodes them with the SatNOGS decoders and delivers them
    as COSMOS telemetry (CANVAS, AEPEX and CatSat to start with).
  EOF
  s.licenses = 'MIT'
  s.authors = ['Anonymous']
  s.email = ['name@domain.com']
  s.homepage = 'https://github.com/OpenC3/cosmos'
  s.platform = Gem::Platform::RUBY
  s.required_ruby_version = '>= 3.0'

  if ENV['VERSION']
    s.version = ENV['VERSION'].dup
  else
    time = Time.now.strftime("%Y%m%d%H%M%S")
    s.version = '0.0.0' + ".#{time}"
  end
  s.files = Dir.glob("{targets,lib}/**/*").reject { |f| f.include?('__pycache__') } +
            %w(Rakefile README.md CHANGELOG.md plugin.txt requirements.txt)
end
